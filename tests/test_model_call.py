from typing import Any

import pytest

from app.agent.loop import HANDOFF_REPLY, AgentResult, run_agent
from app.agent.model_call import DEFAULT_RETRY_AFTER_S, RetryRecord, retry_after_seconds
from app.config import Settings
from app.store.store import Store
from tests.fakes import (
    FakeClock,
    FakeGroq,
    bad_request_error,
    rate_limit_error,
    reply,
    server_error,
    tool_call,
    tool_calls,
    tool_use_failed_error,
)

SETTINGS = Settings(groq_api_key="test-key", request_deadline_s=40, request_timeout_s=20)
HI = [{"role": "user", "content": "hi"}]


class Harness:
    """A fake clock plus a sleep that advances it, so retries take no real time."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.advance(seconds)

    def run(self, client: FakeGroq, settings: Settings = SETTINGS) -> AgentResult:
        return run_agent(HI, Store.fresh(), client, settings, clock=self.clock, sleep=self.sleep)


def test_rate_limit_wait_then_retry_succeeds() -> None:
    h = Harness()
    client = FakeGroq(rate_limit_error("5"), reply("Hello!"))

    result = h.run(client)

    assert result.reply == "Hello!"
    assert result.error is None
    assert h.slept == [5.0]
    assert result.retries == [RetryRecord(reason="rate_limited", wait_s=5.0)]
    assert len(client.requests) == 2


def test_rate_limit_wait_that_does_not_fit_stops_without_sleeping() -> None:
    h = Harness()  # 40 s left: a 21 s wait plus a 20 s call does not fit

    result = h.run(FakeGroq(rate_limit_error("21")))

    assert result.error == "model_busy"
    assert h.slept == []
    assert result.retry_after_s == 21


def test_rate_limit_retries_are_capped_by_max_retries() -> None:
    h = Harness()
    client = FakeGroq(rate_limit_error("1"), rate_limit_error("1"), reply("never"))

    result = h.run(client)  # MAX_RETRIES defaults to 1

    assert result.error == "model_busy"
    assert h.slept == [1.0]
    assert len(client.requests) == 2


def test_server_error_is_retried_with_backoff() -> None:
    h = Harness()
    settings = Settings(groq_api_key="k", max_retries=2)
    client = FakeGroq(server_error(500), server_error(503), reply("Hello!"))

    result = h.run(client, settings)

    assert result.reply == "Hello!"
    assert h.slept == [1.0, 2.0]
    assert [r.reason for r in result.retries] == ["server_error", "server_error"]


def test_server_error_after_retries_is_api_error() -> None:
    result = Harness().run(FakeGroq(server_error(), server_error()))

    assert result.error == "api_error"


def test_other_400s_are_not_retried() -> None:
    client = FakeGroq(bad_request_error())

    result = Harness().run(client)

    assert result.error == "api_error"
    assert len(client.requests) == 1


def test_deadline_is_never_exceeded() -> None:
    h = Harness()
    start = h.clock()
    settings = Settings(groq_api_key="k", request_deadline_s=40, max_retries=5, max_steps=10)
    script: list[Any] = [rate_limit_error("3"), server_error()] * 3
    script += [tool_calls(tool_call("search_products", {"query": "x"}))] * 10
    client = FakeGroq(*script)
    # Every call uses its whole timeout: the worst case for the deadline.
    client.on_call = lambda: h.clock.advance(client.requests[-1]["timeout"])

    result = h.run(client, settings)

    assert h.clock() - start <= 40
    assert result.error in {"deadline", "model_busy", "api_error"}
    assert all(r["timeout"] <= 20 for r in client.requests)


def test_tool_use_failed_once_is_retried_without_using_a_step() -> None:
    h = Harness()
    settings = Settings(groq_api_key="k", max_steps=1)
    client = FakeGroq(tool_use_failed_error(), reply("Here you go."))

    result = h.run(client, settings)

    assert result.reply == "Here you go."
    assert result.error is None
    assert result.retries == [RetryRecord(reason="tool_use_failed", wait_s=0.0)]
    assert h.slept == []


def test_tool_use_failed_twice_hands_off() -> None:
    result = Harness().run(FakeGroq(tool_use_failed_error(), tool_use_failed_error()))

    assert result.error == "tool_call_failed"
    assert result.reply == HANDOFF_REPLY


@pytest.mark.parametrize(
    ("header", "expected"),
    [("7", 7.0), ("2.5", 2.5), (None, DEFAULT_RETRY_AFTER_S), ("soon", DEFAULT_RETRY_AFTER_S)],
)
def test_retry_after_parsing(header: str | None, expected: float) -> None:
    assert retry_after_seconds(rate_limit_error(header)) == expected
