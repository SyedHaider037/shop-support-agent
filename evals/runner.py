"""Run eval tasks against the agent and write one JSONL row per (model, task, trial).

The runner calls run_agent directly with a fresh, date-pinned store per run,
so every trial starts from the same state. Infra problems (rate limits,
outages) are retried and, if they persist, recorded as infra_error: they say
nothing about the agent, so they are never counted as a pass or a fail.
"""

import json
import re
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
from evals.pacer import Paced, PacerStats

TODAY = date(2026, 10, 20)  # the day the sample data was written for
INFRA_ERRORS = frozenset({"model_busy", "api_error", "timeout", "deadline"})
# Groq's per-minute limits clear within a minute; a longer wait means the daily cap.
DAILY_LIMIT_S = 120.0
DAILY_LIMIT_EXIT = 3
INFRA_RETRIES = 2
INFRA_WAIT_S = 10.0
# A simulated customer that ends while the agent is waiting for an answer has stopped
# testing the agent. Its turn is asked again this many times, then the run is an infra error.
EARLY_END_RETRIES = 2
# Infra errors that a fresh attempt of the whole trial would not fix: recorded at once.
NOT_RETRIED = frozenset({"sim_ended_early"})
# A closing "Anything else I can help with?" does not wait for an answer.
_COURTESY = re.compile(
    r"\b(?:anything else|any other (?:questions?|way)|something else|further "
    r"(?:help|assistance|questions?)|help (?:you )?with anything)\b",
    re.IGNORECASE,
)


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


def waits_for_answer(agent_text: str) -> bool:
    """Did the agent's message ask the customer something it needs answered?"""
    questions = re.findall(r"[^.?!]*\?", agent_text)
    return any(not _COURTESY.search(question) for question in questions)


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
    agent_retries: list[dict[str, Any]]  # the agent loop's own 429/5xx/tool_use_failed retries
    forced_retries: list[dict[str, Any]]  # forced tool calls: turn, reason, first reply (masked)
    # Per agent reply: its text before the output guardrails (masked), or None if unchanged.
    replies_before_guardrails: list[str | None]
    sim_retries: int  # simulator calls retried after a stray tool call (not infra attempts)
    # Simulator turns asked again because it ended while the agent waited for an answer.
    sim_early_end_retries: int = 0


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
    retries: list[dict[str, Any]] = []
    forced: list[dict[str, Any]] = []
    before: list[str | None] = []
    early_end_retries = 0
    for turn in range(1, max_turns + 1):
        text = user.next_message(history)
        waiting = (
            task.customer is not None and outcome.replies and waits_for_answer(outcome.replies[-1])
        )
        if text is None and waiting:
            for _ in range(EARLY_END_RETRIES):
                early_end_retries += 1
                text = user.next_message(history)
                if text is not None:
                    break
            else:
                raise InfraError("sim_ended_early")
        if text is None:
            break
        history.append({"role": "user", "content": text})
        outcome.user_texts.append(text)
        result = run_agent(history, store, client, settings, clock=clock, sleep=sleep)
        retries += [{"turn": turn, **asdict(retry)} for retry in result.retries]
        if result.forced_retry_reason:
            forced.append(
                {
                    "turn": turn,
                    "reason": result.forced_retry_reason,
                    "first_reply": mask_emails(result.first_reply or ""),
                    "recovered_error": result.recovered_error,
                }
            )
        if result.error in INFRA_ERRORS:
            raise InfraError(result.error or "api_error", result.retry_after_s)
        if result.error:
            agent_errors.append(result.error)
            outcome.agent_errors.append(result.error)
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
        raw = result.reply_before_guardrails
        before.append(mask_emails(raw) if raw is not None and raw != result.reply else None)
        history.append({"role": "assistant", "content": result.reply})
    if task.customer is not None and not outcome.replies:
        # The simulated customer ended before the agent said anything: nothing was tested.
        raise InfraError("sim_empty_conversation")
    sim_retries = getattr(user, "retries", 0)
    return Conversation(
        outcome,
        history,
        agent_errors,
        events,
        retries,
        forced,
        before,
        sim_retries,
        early_end_retries,
    )


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


@dataclass
class Attempt:
    """One try at a trial. Failed tries still spend tokens, so they are kept."""

    attempt: int
    status: str  # "ok" or the infra error code
    retry_after_s: float | None
    wait_before_s: float  # infra wait slept before this try
    agent: PacerStats
    sim: PacerStats

    def summary(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "status": self.status,
            "reason": None if self.status == "ok" else self.status,
            "retry_after_s": self.retry_after_s,
            "wait_before_s": self.wait_before_s,
            "tokens": _tokens(self.agent),
            "sim_tokens": _tokens(self.sim),
            "pacer_waited_s": round(self.agent.waited_s + self.sim.waited_s, 3),
        }


def _attempt_fields(attempts: list[Attempt], config: RunConfig) -> dict[str, Any]:
    """Row fields covering every try, so hidden retries and their cost are visible."""

    def total(stats: list[PacerStats]) -> dict[str, int]:
        return {
            key: sum(_tokens(s)[key] for s in stats) for key in ("prompt", "completion", "cached")
        }

    cost = sum(
        _cost(config.settings.model, a.agent) + _cost(config.sim_model, a.sim) for a in attempts
    )
    return {
        "attempts": [a.summary() for a in attempts],
        "infra_retry_waits_s": [a.wait_before_s for a in attempts if a.attempt > 1],
        "tokens_all_attempts": {
            "agent": total([a.agent for a in attempts]),
            "sim": total([a.sim for a in attempts]),
        },
        "estimated_cost_usd": round(cost, 8),  # every attempt, not just the graded one
    }


def _take(agent: Paced, sim: Paced | None) -> tuple[PacerStats, PacerStats]:
    return agent.take_stats(), sim.take_stats() if sim else PacerStats()


def run_task(
    task: Task,
    trial: int,
    config: RunConfig,
    agent: Paced,
    make_user: UserFactory,
    sim: Paced | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One trial, with infra retries. Raises DailyLimitReached; never raises for infra errors."""
    settings = config.settings
    error: InfraError | None = None
    attempts: list[Attempt] = []
    for attempt in range(1, INFRA_RETRIES + 2):
        wait = 0.0
        if attempt > 1:
            # Wait as long as Groq asked (agent or simulator), and at least INFRA_WAIT_S.
            asked = error.retry_after_s if error and error.retry_after_s is not None else -1
            wait = max(asked + 1, INFRA_WAIT_S)
            sleep(wait)
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
            attempts.append(Attempt(attempt, exc.code, exc.retry_after_s, wait, *_take(agent, sim)))
            if exc.retry_after_s is not None and exc.retry_after_s > DAILY_LIMIT_S:
                raise DailyLimitReached(
                    f"{exc.code}: retry after {exc.retry_after_s:.0f}s"
                ) from exc
            error = exc
            if exc.code in NOT_RETRIED:
                break
            continue
        latency_ms = round((clock() - started) * 1000)
        attempts.append(Attempt(attempt, "ok", None, wait, *_take(agent, sim)))
        return _row(task, trial, config, convo, attempts, latency_ms)
    return _infra_row(task, trial, config, error.code if error else "infra_error", attempts)


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


def _infra_row(
    task: Task, trial: int, config: RunConfig, code: str, attempts: list[Attempt]
) -> dict[str, Any]:
    return {
        **_base(task, trial, config),
        "status": "infra_error",
        "error": code,
        **_attempt_fields(attempts, config),
    }


def _row(
    task: Task,
    trial: int,
    config: RunConfig,
    convo: Conversation,
    attempts: list[Attempt],
    latency_ms: int,
) -> dict[str, Any]:
    failures = grade(task, convo.outcome)
    agent_stats, sim_stats = attempts[-1].agent, attempts[-1].sim
    replies = convo.outcome.replies
    return {
        **_base(task, trial, config),
        "status": "fail" if failures else "pass",
        "error": None,
        "failed_checks": [asdict(f) for f in failures],
        "first_failure_layer": first_failure_layer(failures, convo.outcome),
        "agent_errors": convo.agent_errors,
        "forced_retries": convo.forced_retries,
        "replies_before_guardrails": convo.replies_before_guardrails,
        "sim_retries": convo.sim_retries,
        "sim_early_end_retries": convo.sim_early_end_retries,
        # Sample emails are fake, but masked anyway so results never collect contact details.
        "final_reply": mask_emails(replies[-1]) if replies else None,
        "transcript": mask_emails(convo.transcript),
        "tool_calls": [asdict(c) for c in convo.outcome.tool_calls],
        "guardrail_events": convo.guardrail_events,
        "steps": convo.outcome.model_calls,
        "agent_retries": convo.agent_retries,
        # Tokens, latency and pacer data of the graded (last) try; every try is in attempts.
        "tokens": _tokens(agent_stats),
        "sim_tokens": _tokens(sim_stats),
        "latency_ms": latency_ms,
        "pacer_wait_ms": round((agent_stats.waited_s + sim_stats.waited_s) * 1000),
        "pacer": {
            name: {"waited_s": round(stats.waited_s, 3), "calls": [asdict(c) for c in stats.calls]}
            for name, stats in (("agent", agent_stats), ("sim", sim_stats))
        },
        **_attempt_fields(attempts, config),
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
    agent: Paced,
    make_user: UserFactory,
    sim: Paced | None = None,
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
