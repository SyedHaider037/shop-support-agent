"""pass@1, pass@k and pass^k from result rows, plus a Markdown summary.

pass@k asks "did it ever work?"; pass^k asks "does it work every time?". A
support agent must be reliable, so pass^k is the headline. Infra errors
(rate limits, outages) are neither pass nor fail and are reported separately.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

Row = Mapping[str, Any]


@dataclass
class TaskStats:
    model: str
    task_id: str
    category: str
    suite: str
    graded: int = 0  # trials that ended in pass or fail
    passes: int = 0
    infra_errors: int = 0
    infra_reasons: list[str] = field(default_factory=list)  # e.g. model_busy, sim_invented_fact
    failed_checks: list[str] = field(default_factory=list)
    failure_layers: list[str] = field(default_factory=list)

    @property
    def pass_at_1(self) -> float | None:
        return self.passes / self.graded if self.graded else None

    def complete(self, k: int) -> bool:
        return self.graded >= k

    def pass_at_k(self, k: int) -> bool | None:
        """At least one of k trials passed (None if fewer than k trials were graded)."""
        return self.passes >= 1 if self.complete(k) else None

    def pass_hat_k(self, k: int) -> bool | None:
        """All k trials passed (None if fewer than k trials were graded)."""
        return self.passes == self.graded if self.complete(k) else None


@dataclass
class GroupSummary:
    name: str
    tasks: int
    pass_at_1: float | None
    pass_at_k: float | None
    pass_hat_k: float | None
    incomplete: int  # tasks with fewer than k graded trials
    infra_errors: int


def expected_pass_hat_k(p: float, k: int) -> float:
    """If each trial passes independently with probability p, all k pass with p**k."""
    return p**k


def latest_rows(rows: Iterable[Row]) -> list[Row]:
    """Keep the last row per (model, task, trial): a re-run replaces an infra error."""
    latest: dict[tuple[str, str, int], Row] = {}
    for row in rows:
        latest[(row["model"], row["task_id"], row["trial"])] = row
    return list(latest.values())


def task_stats(rows: Iterable[Row]) -> list[TaskStats]:
    stats: dict[tuple[str, str], TaskStats] = {}
    for row in sorted(latest_rows(rows), key=lambda r: (r["model"], r["task_id"], r["trial"])):
        key = (row["model"], row["task_id"])
        s = stats.setdefault(
            key, TaskStats(row["model"], row["task_id"], row["category"], row["suite"])
        )
        if row["status"] == "infra_error":
            s.infra_errors += 1
            s.infra_reasons.append(row.get("error") or "unknown")
            continue
        s.graded += 1
        if row["status"] == "pass":
            s.passes += 1
        else:
            s.failed_checks += [f["check"] for f in row.get("failed_checks", [])]
            if row.get("first_failure_layer"):
                s.failure_layers.append(row["first_failure_layer"])
    return list(stats.values())


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarise(name: str, stats: list[TaskStats], k: int) -> GroupSummary:
    """Task-level means: every task counts once, however many trials it had."""
    complete = [s for s in stats if s.complete(k)]
    return GroupSummary(
        name=name,
        tasks=len(stats),
        pass_at_1=_mean([s.pass_at_1 for s in stats if s.pass_at_1 is not None]),
        pass_at_k=_mean([float(bool(s.pass_at_k(k))) for s in complete]),
        pass_hat_k=_mean([float(bool(s.pass_hat_k(k))) for s in complete]),
        incomplete=len(stats) - len(complete),
        infra_errors=sum(s.infra_errors for s in stats),
    )


def summarise_by(stats: list[TaskStats], k: int, attribute: str) -> list[GroupSummary]:
    groups: dict[str, list[TaskStats]] = {}
    for s in stats:
        groups.setdefault(getattr(s, attribute), []).append(s)
    return [summarise(name, members, k) for name, members in sorted(groups.items())]


def suite_pass_rate(stats: list[TaskStats], suite: str) -> float | None:
    """pass@1 for one suite, used by --fail-under as a CI gate."""
    return summarise(suite, [s for s in stats if s.suite == suite], k=1).pass_at_1


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def markdown_summary(rows: Iterable[Row], k: int) -> str:
    """One section per model: a table by category and suite, then every failed task."""
    stats = task_stats(rows)
    out: list[str] = []
    for model in sorted({s.model for s in stats}):
        mine = [s for s in stats if s.model == model]
        out += [f"## {model}", "", f"k = {k} trials per task.", ""]
        out += [
            f"| Group | Tasks | pass@1 | pass@{k} | pass^{k} | Incomplete | Infra errors |",
            "|---|---|---|---|---|---|---|",
        ]
        groups = (
            summarise_by(mine, k, "category")
            + summarise_by(mine, k, "suite")
            + [summarise("**overall**", mine, k)]
        )
        for g in groups:
            out.append(
                f"| {g.name} | {g.tasks} | {_pct(g.pass_at_1)} | {_pct(g.pass_at_k)} | "
                f"{_pct(g.pass_hat_k)} | {g.incomplete} | {g.infra_errors} |"
            )
        failing = [s for s in mine if s.graded and s.passes < s.graded]
        out += ["", "### Failed tasks", ""]
        if not failing:
            out.append("None.")
        for s in sorted(failing, key=lambda s: s.task_id):
            checks = ", ".join(sorted(set(s.failed_checks)))
            layers = ", ".join(sorted(set(s.failure_layers))) or "n/a"
            out.append(
                f"- `{s.task_id}` ({s.category}, {s.suite}): passed {s.passes}/{s.graded}; "
                f"failed checks: {checks}; first failure layer (hint): {layers}"
            )
        # Not agent failures, but shown so a run hiding many of them is obvious.
        infra = [s for s in mine if s.infra_errors]
        out += ["", "### Infra errors (not counted as pass or fail)", ""]
        if not infra:
            out.append("None.")
        for s in sorted(infra, key=lambda s: s.task_id):
            reasons = ", ".join(
                f"{r} x{s.infra_reasons.count(r)}" for r in sorted(set(s.infra_reasons))
            )
            out.append(f"- `{s.task_id}`: {reasons}")
        out.append("")
    return "\n".join(out)
