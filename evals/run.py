"""CLI: python -m evals.run --model MODEL --trials N [--tasks ID,ID] [--suite S] [--category C]

Exit codes: 0 done, 1 regression pass rate under --fail-under,
3 Groq's daily limit was reached (progress saved; run the same command again).
"""

import argparse
import dataclasses
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv

from app.config import create_groq_client, load_settings
from evals.metrics import markdown_summary, suite_pass_rate, task_stats
from evals.models import TASKS_PATH, Task, load_tasks
from evals.pacer import Pacer
from evals.runner import (
    DAILY_LIMIT_EXIT,
    RunConfig,
    UserFactory,
    finished,
    plan,
    read_rows,
    run_all,
    scripted_only,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "openai/gpt-oss-120b"
SIM_MODEL = "openai/gpt-oss-20b"
# Rough per-turn costs from the live checks, for --dry-run estimates only.
TOKENS_PER_AGENT_TURN = 2_700
TOKENS_PER_SIM_TURN = 600


def git_commit() -> str:
    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        return done.stdout.strip() if done.returncode == 0 else ""

    commit = git("rev-parse", "--short", "HEAD") or "unknown"
    return commit + ("-dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def select(tasks: list[Task], ids: str | None, suite: str, category: str | None) -> list[Task]:
    wanted = {i.strip() for i in ids.split(",")} if ids else None
    if wanted:
        unknown = wanted - {t.id for t in tasks}
        if unknown:
            raise SystemExit(f"unknown task ids: {sorted(unknown)}")
    return [
        t
        for t in tasks
        if (wanted is None or t.id in wanted)
        and (suite == "all" or t.suite == suite)
        and (category is None or t.category == category)
    ]


def estimate_tokens(task: Task) -> int:
    if task.customer:
        return task.customer.max_turns * (TOKENS_PER_AGENT_TURN + TOKENS_PER_SIM_TURN)
    return len(task.turns) * TOKENS_PER_AGENT_TURN


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--sim-model", default=SIM_MODEL)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--tasks", help="comma-separated task ids")
    parser.add_argument("--suite", choices=["regression", "capability", "all"], default="all")
    parser.add_argument("--category")
    parser.add_argument("--tasks-file", type=Path, default=TASKS_PATH)
    parser.add_argument("--out", type=Path, default=ROOT / "evals" / "results" / "run.jsonl")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, call nothing")
    parser.add_argument(
        "--fail-under", type=float, help="exit 1 if regression pass@1 is below this (0-1)"
    )
    return parser.parse_args(argv)


def user_factory(sim: Pacer | None, sim_model: str) -> UserFactory:
    """Scripted tasks need no model; simulated customers are added by evals.customer_sim."""
    return scripted_only


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(ROOT / ".env")
    settings = dataclasses.replace(load_settings(), model=args.model, reasoning_effort="low")
    tasks = select(load_tasks(args.tasks_file), args.tasks, args.suite, args.category)
    done = finished(read_rows(args.out))
    todo = plan(tasks, args.trials, settings.model, done)
    skipped = len(tasks) * args.trials - len(todo)

    estimate = sum(estimate_tokens(task) for task, _ in todo)
    print(
        f"{len(todo)} runs planned ({skipped} already graded in {args.out.name}), model "
        f"{settings.model}, about {estimate:,} tokens (~{estimate / 6_500:.0f} min at 6,500 TPM)."
    )
    if args.dry_run:
        for task, trial in todo:
            kind = "simulated" if task.simulated else f"{len(task.turns)} turn(s)"
            print(f"  {task.id} #{trial} [{task.category}/{task.suite}, {kind}]")
        return 0

    exit_code = 0
    if todo:
        client = create_groq_client(settings)  # raises a clear ConfigError without a key
        agent = Pacer(client)
        sim = Pacer(client) if any(t.simulated for t, _ in todo) else None
        config = RunConfig(settings=settings, commit=git_commit(), sim_model=args.sim_model)
        completed = run_all(todo, args.out, config, agent, user_factory(sim, args.sim_model), sim)
        if not completed:
            exit_code = DAILY_LIMIT_EXIT

    rows = [r for r in read_rows(args.out) if r["model"] == settings.model]
    summary_path = args.out.with_suffix(".md")
    summary_path.write_text(markdown_summary(rows, k=args.trials), encoding="utf-8")
    print(f"Summary written to {summary_path}")

    if args.fail_under is not None and exit_code == 0:
        rate = suite_pass_rate(task_stats(rows), "regression")
        print(f"Regression pass@1: {'n/a' if rate is None else f'{rate:.0%}'}")
        if rate is None or rate < args.fail_under:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
