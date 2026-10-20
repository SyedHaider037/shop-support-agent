from types import SimpleNamespace

import pytest

from evals.pacer import Pacer
from tests.fakes import FakeClock, FakeGroq, rate_limit_error, reply


def response(prompt: int, completion: int = 100, cached: int = 0) -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )
    return SimpleNamespace(choices=reply("ok").choices, usage=usage)


class Harness:
    def __init__(self, *script: object, tpm: int = 6_500, rpm: int = 25) -> None:
        self.clock = FakeClock()
        self.slept: list[float] = []
        self.fake = FakeGroq(*script)
        self.pacer = Pacer(
            self.fake, tpm_budget=tpm, rpm_budget=rpm, clock=self.clock, sleep=self.sleep
        )

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.advance(seconds)

    def call(self) -> object:
        return self.pacer.chat.completions.create(model="m", messages=[])


def test_first_call_does_not_wait() -> None:
    h = Harness(response(1_000))

    h.call()

    assert h.slept == []
    assert h.pacer.estimate() == 1_400  # last prompt + margin


def test_unknown_estimate_is_2000() -> None:
    assert Harness().pacer.estimate() == 2_000


def test_waits_until_the_token_window_has_room() -> None:
    # 3 calls of 2,000 tokens fill 6,000 of 6,500; the 4th (estimate 1,900+400) must wait.
    h = Harness(*[response(1_900) for _ in range(4)])
    for _ in range(3):
        h.call()
        h.clock.advance(1)

    h.call()

    assert len(h.slept) == 1
    assert h.slept[0] == pytest.approx(60 - 3 + 0.05)
    assert h.pacer.stats.waited_s == pytest.approx(h.slept[0])


def test_cached_tokens_do_not_count_against_the_window() -> None:
    h = Harness(*[response(2_000, completion=100, cached=1_800) for _ in range(5)])

    for _ in range(5):
        h.call()

    assert h.slept == []
    assert h.pacer.stats.cached_tokens == 9_000
    assert h.pacer.stats.prompt_tokens == 10_000


def test_requests_per_minute_are_limited() -> None:
    h = Harness(*[response(10, completion=1) for _ in range(4)], rpm=3)

    for _ in range(4):
        h.call()

    assert len(h.slept) == 1  # the 4th request waits for the 1st to leave the window


def test_a_call_larger_than_the_budget_goes_once_the_window_is_empty() -> None:
    h = Harness(response(9_000), response(9_000))

    h.call()
    h.call()

    assert len(h.slept) == 1
    assert h.slept[0] == pytest.approx(60.05)


def test_failed_calls_count_as_requests_and_errors_propagate() -> None:
    h = Harness(rate_limit_error("5"))

    with pytest.raises(Exception, match="rate limited"):
        h.call()

    assert h.pacer.stats.requests == 1
    assert h.pacer.stats.prompt_tokens == 0


def test_arguments_pass_through_and_stats_reset() -> None:
    h = Harness(response(500, completion=50))

    h.pacer.chat.completions.create(model="m", messages=[{"role": "user"}], timeout=12)
    stats = h.pacer.take_stats()

    assert h.fake.requests[0]["timeout"] == 12
    assert (stats.prompt_tokens, stats.completion_tokens, stats.requests) == (500, 50, 1)
    assert h.pacer.stats.requests == 0


def test_total_wait_is_kept_across_stat_resets() -> None:
    h = Harness(response(9_000), response(9_000))
    h.call()
    h.pacer.take_stats()

    h.call()

    assert h.pacer.total_waited_s == pytest.approx(60.05)


def test_each_call_records_what_the_pacer_saw() -> None:
    h = Harness(response(1_000, completion=100, cached=600), response(1_200, completion=50))
    h.call()
    h.clock.advance(1)

    h.call()

    first, second = h.pacer.stats.calls
    assert (first.estimate, first.window_tokens, first.window_requests, first.waited_s) == (
        2_000,
        0,
        0,
        0.0,
    )
    assert (first.prompt, first.cached, first.completion) == (1_000, 600, 100)
    assert (second.estimate, second.window_tokens, second.window_requests) == (1_400, 500, 1)


def test_a_failed_call_is_recorded_with_its_error() -> None:
    h = Harness(rate_limit_error("5"))

    with pytest.raises(Exception, match="rate limited"):
        h.call()

    [call] = h.pacer.stats.calls
    assert call.error == "RateLimitError"
    assert call.prompt == 0


def test_the_recorded_smoke_test_calls_do_not_make_the_pacer_wait() -> None:
    # Investigation of the 27.6 s wait in the Phase 4A smoke test: the real per-call
    # numbers (cache hits come in 1,024-token blocks) never fill the 6,500-token window,
    # so the estimate counting cached tokens does not explain it on its own.
    calls = [
        (0.0, 1230, 45, 1024),
        (0.7, 1326, 46, 1024),
        (2.0, 1240, 50, 1024),
        (5.0, 1300, 50, 1024),
        (8.0, 1391, 52, 0),
        (10.0, 1246, 73, 1024),
        (11.2, 1318, 39, 1024),
        (12.1, 1260, 68, 0),
    ]
    h = Harness(*[response(p, completion=c, cached=k) for _, p, c, k in calls])

    for start, *_ in calls:
        h.clock.now = 1_000.0 + start
        h.call()

    assert h.slept == []
    assert max(c.window_tokens + c.estimate for c in h.pacer.stats.calls) < 6_500
