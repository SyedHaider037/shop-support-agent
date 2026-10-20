from itertools import product
from typing import Any

import pytest

from evals.metrics import (
    expected_pass_hat_k,
    latest_rows,
    markdown_summary,
    suite_pass_rate,
    summarise,
    summarise_by,
    task_stats,
)


def row(
    task_id: str,
    trial: int,
    status: str,
    category: str = "orders",
    suite: str = "regression",
    model: str = "m",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "model": model,
        "task_id": task_id,
        "trial": trial,
        "status": status,
        "category": category,
        "suite": suite,
        **extra,
    }


def test_pass_at_1_pass_at_k_and_pass_hat_k_per_task() -> None:
    rows = [row("a", 1, "pass"), row("a", 2, "fail"), row("a", 3, "pass")]
    rows += [row("b", t, "pass") for t in (1, 2, 3)]
    rows += [row("c", t, "fail") for t in (1, 2, 3)]
    stats = {s.task_id: s for s in task_stats(rows)}

    assert stats["a"].pass_at_1 == pytest.approx(2 / 3)
    assert (stats["a"].pass_at_k(3), stats["a"].pass_hat_k(3)) == (True, False)
    assert (stats["b"].pass_at_k(3), stats["b"].pass_hat_k(3)) == (True, True)
    assert (stats["c"].pass_at_k(3), stats["c"].pass_hat_k(3)) == (False, False)

    overall = summarise("all", list(stats.values()), k=3)
    assert overall.pass_at_1 == pytest.approx((2 / 3 + 1 + 0) / 3)
    assert overall.pass_at_k == pytest.approx(2 / 3)
    assert overall.pass_hat_k == pytest.approx(1 / 3)


def test_75_percent_per_trial_gives_about_42_percent_pass_hat_3() -> None:
    # Every combination of 3 trials where each trial passes in 3 of 4 outcomes: this is
    # exactly independent 75% success per trial, so pass^3 must be 0.75 ** 3.
    rows = []
    for index, outcome in enumerate(product(range(4), repeat=3)):
        for trial, value in enumerate(outcome, start=1):
            rows.append(row(f"t{index}", trial, "pass" if value < 3 else "fail"))

    overall = summarise("all", task_stats(rows), k=3)

    assert overall.pass_at_1 == pytest.approx(0.75)
    assert overall.pass_hat_k == pytest.approx(0.421875)
    assert overall.pass_hat_k == pytest.approx(expected_pass_hat_k(0.75, 3))
    assert round(overall.pass_hat_k * 100) == 42


def test_infra_errors_are_neither_pass_nor_fail() -> None:
    rows = [row("a", 1, "pass"), row("a", 2, "infra_error"), row("a", 3, "pass")]
    [stats] = task_stats(rows)

    assert (stats.graded, stats.passes, stats.infra_errors) == (2, 2, 1)
    assert stats.pass_at_1 == 1.0
    # Only 2 of 3 trials graded: pass^3 is unknown, not a pass.
    assert stats.pass_hat_k(3) is None
    summary = summarise("all", [stats], k=3)
    assert (summary.pass_hat_k, summary.incomplete, summary.infra_errors) == (None, 1, 1)


def test_task_with_only_infra_errors_has_no_pass_rate() -> None:
    [stats] = task_stats([row("a", 1, "infra_error")])

    assert stats.pass_at_1 is None
    assert summarise("all", [stats], k=1).pass_at_1 is None


def test_a_rerun_replaces_the_infra_error_row() -> None:
    rows = [row("a", 1, "infra_error"), row("a", 1, "pass")]

    assert [r["status"] for r in latest_rows(rows)] == ["pass"]
    [stats] = task_stats(rows)
    assert (stats.graded, stats.passes, stats.infra_errors) == (1, 1, 0)


def test_grouping_by_category_and_suite() -> None:
    rows = [
        row("a", 1, "pass", category="orders", suite="regression"),
        row("b", 1, "fail", category="orders", suite="capability"),
        row("c", 1, "pass", category="refunds", suite="regression"),
    ]
    stats = task_stats(rows)

    by_category = {g.name: g.pass_at_1 for g in summarise_by(stats, 1, "category")}
    by_suite = {g.name: g.pass_at_1 for g in summarise_by(stats, 1, "suite")}

    assert by_category == {"orders": 0.5, "refunds": 1.0}
    assert by_suite == {"capability": 0.0, "regression": 1.0}
    assert suite_pass_rate(stats, "regression") == 1.0
    assert suite_pass_rate(stats, "capability") == 0.0
    assert suite_pass_rate(stats, "missing") is None


def test_models_are_kept_apart() -> None:
    rows = [row("a", 1, "pass", model="m1"), row("a", 1, "fail", model="m2")]

    assert {(s.model, s.passes) for s in task_stats(rows)} == {("m1", 1), ("m2", 0)}


def test_markdown_summary_lists_groups_and_failed_tasks() -> None:
    failure = [{"check": "must_match_any", "reason": "x"}]
    rows = [
        row("ok-01", 1, "pass"),
        row(
            "bad-01",
            1,
            "fail",
            category="refunds",
            failed_checks=failure,
            first_failure_layer="tool_refused_reply_wrong",
        ),
    ]

    text = markdown_summary(rows, k=1)

    assert "## m" in text
    assert "| orders | 1 | 100% | 100% | 100% | 0 | 0 |" in text
    assert "| refunds | 1 | 0% | 0% | 0% | 0 | 0 |" in text
    assert "| **overall** | 2 | 50% |" in text
    assert "`bad-01` (refunds, regression): passed 0/1; failed checks: must_match_any" in text
    assert "tool_refused_reply_wrong" in text
    assert "ok-01" not in text.split("### Failed tasks")[1]


def test_markdown_summary_with_no_failures() -> None:
    assert "None." in markdown_summary([row("a", 1, "pass")], k=1)
