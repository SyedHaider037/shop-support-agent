"""Run eval tasks against the agent and write one JSONL row per (model, task, trial).

The runner calls run_agent directly with a fresh, date-pinned store per run,
so every trial starts from the same state. Infra problems (rate limits,
outages) are retried and, if they persist, recorded as infra_error: they say
nothing about the agent, so they are never counted as a pass or a fail.
"""

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from app.agent.loop import run_agent
from app.config import Settings
from app.store.store import Store
from app.trace.redact import mask_emails
from app.trace.trace import estimated_cost_usd
from evals.graders import first_failure_layer, grade
from evals.models import Outcome, Task, ToolCallRecord
from evals.pacer import Pacer, PacerStats

TODAY = date(2026, 10, 20)  # the day the sample data was written for
INFRA_ERRORS = frozenset({"model_busy", "api_error", "timeout", "deadline"})
# Groq's per-minute limits clear within a minute; a longer wait means the daily cap.
DAILY_LIMIT_S = 120.0
DAILY_LIMIT_EXIT = 3
INFRA_RETRIES = 2
INFRA_WAIT_S = 10.0


class InfraError(Exception):
    def __init__(self, code: str, retry_after_s: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after_s = retry_after_s


class DailyLimitReached(Exception):
    """Groq asked us to wait so long that it must be the daily quota."""


class UserSide(Protocol):
    """Who writes the user's messages: a script or a simulated customer."""

    def next_message(self, history: list[dict[str, str]]) -> str | None: ...


class ScriptedUser:
    def __init__(self, turns: list[str]) -> None:
        self._turns: Iterator[str] = iter(turns)

    def next_message(self, history: list[dict[str, str]]) -> str | None:
        return next(self._turns, None)


UserFactory = Callable[[Task], UserSide]


def scripted_only(task: Task) -> UserSide:
    if task.customer is not None:
        raise InfraError("no_simulator")
    return ScriptedUser(task.turns)


@dataclass
class RunConfig:
    settings: Settings  # model, effort and limits, exactly as production uses them
    commit: str
    sim_model: str | None = None
    today: date = TODAY


@dataclass
class Conversation:
    outcome: Outcome
    transcript: list[dict[str, str]]
    agent_errors: list[str]
    guardrail_events: list[dict[str, str]]


def converse(
    task: Task,
    store: Store,
    user: UserSide,
    client: Any,
    settings: Settings,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    max_turns: int,
) -> Conversation:
    """Play the conversation turn by turn, sending the full history each time like the client."""
    history = [m.model_dump() for m in task.history]
    outcome = Outcome(
        store=store,
        user_texts=[m["content"] for m in history if m["role"] == "user"],
        approvals_before=len(store.list_approvals()),
    )
    agent_errors: list[str] = []
    events: list[dict[str, str]] = []
    for _ in range(max_turns):
        text = user.next_message(history)
        if text is None:
            break
        history.append({"role": "user", "content": text})
        outcome.user_texts.append(text)
        result = run_agent(history, store, client, settings, clock=clock, sleep=sleep)
        if result.error in INFRA_ERRORS:
            raise InfraError(result.error or "api_error", result.retry_after_s)
        if result.error:
            agent_errors.append(result.error)
        outcome.model_calls += result.usage.model_calls
        outcome.tool_calls += [
            ToolCallRecord(
                s.tool, s.arguments, "ok" if s.result.get("ok") else s.result.get("error", "error")
            )
            for s in result.steps
        ]
        for event in result.guardrail_events:
            events.append(asdict(event))
            if event.layer == "input":
                outcome.input_rules_fired.append(event.rule)
        outcome.replies.append(result.reply)
        history.append({"role": "assistant", "content": result.reply})
    return Conversation(outcome, history, agent_errors, events)


def apply_setup(task: Task, store: Store) -> None:
    for request in task.setup.refund_requests:
        store.create_refund_approval(request.order_id, request.email, request.reason)


def _tokens(stats: PacerStats) -> dict[str, int]:
    return {
        "prompt": stats.prompt_tokens,
        "completion": stats.completion_tokens,
        "cached": stats.cached_tokens,
    }


def _cost(model: str | None, stats: PacerStats) -> float:
    if model is None:
        return 0.0
    return estimated_cost_usd(model, stats.prompt_tokens, stats.completion_tokens) or 0.0


def run_task(
    task: Task,
    trial: int,
    config: RunConfig,
    agent: Pacer,
    make_user: UserFactory,
    sim: Pacer | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One trial, with infra retries. Raises DailyLimitReached; never raises for infra errors."""
    settings = config.settings
    error: InfraError | None = None
    for attempt in range(INFRA_RETRIES + 1):
        if attempt:
            wait = (error.retry_after_s or 0) + 1 if error and error.code == "model_busy" else 0
            sleep(max(wait, INFRA_WAIT_S))
        agent.take_stats()
        if sim:
            sim.take_stats()
        started = clock()
        store = Store.fresh(today=config.today)
        apply_setup(task, store)
        # Pacer waits are ours, not the agent's: keep them out of the request deadline.
        agent_clock = lambda: clock() - agent.total_waited_s  # noqa: E731
        max_turns = task.customer.max_turns if task.customer else len(task.turns)
        try:
            convo = converse(
                task, store, make_user(task), agent, settings, agent_clock, sleep, max_turns
            )
        except InfraError as exc:
            if exc.retry_after_s is not None and exc.retry_after_s > DAILY_LIMIT_S:
                raise DailyLimitReached(
                    f"{exc.code}: retry after {exc.retry_after_s:.0f}s"
                ) from exc
            error = exc
            continue
        return _row(task, trial, config, convo, agent, sim, started, clock)
    return _infra_row(task, trial, config, error.code if error else "infra_error")


def _base(task: Task, trial: int, config: RunConfig) -> dict[str, Any]:
    s = config.settings
    return {
        "model": s.model,
        "effort": s.reasoning_effort if s.uses_reasoning_effort else None,
        "temperature": s.temperature,
        "commit": config.commit,
        "run_date": datetime.now(UTC).isoformat(timespec="seconds"),
        "task_id": task.id,
        "category": task.category,
        "suite": task.suite,
        "simulated": task.simulated,
        "trial": trial,
    }


def _infra_row(task: Task, trial: int, config: RunConfig, code: str) -> dict[str, Any]:
    return {**_base(task, trial, config), "status": "infra_error", "error": code}


def _row(
    task: Task,
    trial: int,
    config: RunConfig,
    convo: Conversation,
    agent: Pacer,
    sim: Pacer | None,
    started: float,
    clock: Callable[[], float],
) -> dict[str, Any]:
    failures = grade(task, convo.outcome)
    agent_stats = agent.take_stats()
    sim_stats = sim.take_stats() if sim else PacerStats()
    replies = convo.outcome.replies
    return {
        **_base(task, trial, config),
        "status": "fail" if failures else "pass",
        "error": None,
        "failed_checks": [asdict(f) for f in failures],
        "first_failure_layer": first_failure_layer(failures, convo.outcome),
        "agent_errors": convo.agent_errors,
        # Sample emails are fake, but masked anyway so results never collect contact details.
        "final_reply": mask_emails(replies[-1]) if replies else None,
        "transcript": mask_emails(convo.transcript),
        "tool_calls": [asdict(c) for c in convo.outcome.tool_calls],
        "guardrail_events": convo.guardrail_events,
        "steps": convo.outcome.model_calls,
        "tokens": _tokens(agent_stats),
        "sim_tokens": _tokens(sim_stats),
        "latency_ms": round((clock() - started) * 1000),
        "pacer_wait_ms": round((agent_stats.waited_s + sim_stats.waited_s) * 1000),
        "estimated_cost_usd": round(
            _cost(config.settings.model, agent_stats) + _cost(config.sim_model, sim_stats), 8
        ),
    }


# --- result files ---------------------------------------------------------------


def read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def finished(rows: list[dict[str, Any]]) -> set[tuple[str, str, int]]:
    """(model, task, trial) already graded. Infra errors are not finished: they re-run."""
    return {(r["model"], r["task_id"], r["trial"]) for r in rows if r["status"] in ("pass", "fail")}


def append_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def plan(
    tasks: list[Task], trials: int, model: str, done: set[tuple[str, str, int]]
) -> list[tuple[Task, int]]:
    return [
        (task, trial)
        for task in tasks
        for trial in range(1, trials + 1)
        if (model, task.id, trial) not in done
    ]


def run_all(
    todo: list[tuple[Task, int]],
    out: Path,
    config: RunConfig,
    agent: Pacer,
    make_user: UserFactory,
    sim: Pacer | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = print,
) -> bool:
    """Run every planned trial, saving each row as it finishes. False if the daily limit hit."""
    total_tokens = 0
    for index, (task, trial) in enumerate(todo, start=1):
        try:
            row = run_task(task, trial, config, agent, make_user, sim, clock, sleep)
        except DailyLimitReached as exc:
            echo(f"Daily limit reached ({exc}). Progress is saved; resume tomorrow.")
            return False
        append_row(out, row)
        used = sum(
            row.get(key, {}).get("prompt", 0) + row.get(key, {}).get("completion", 0)
            for key in ("tokens", "sim_tokens")
        )
        total_tokens += used
        detail = row.get("error") or ", ".join(f["check"] for f in row.get("failed_checks", []))
        echo(
            f"[{index}/{len(todo)}] {task.id} #{trial}: {row['status']}"
            + (f" ({detail})" if detail else "")
            + f" | {used} tokens, waited {row.get('pacer_wait_ms', 0) / 1000:.0f}s"
            + f" | running total {total_tokens}"
        )
    return True
