from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import RATE_LIMITED, create_app
from app.ratelimit import RateLimiter, retry_after_header
from app.store.store import Store
from tests.fakes import FakeClock, FakeGroq, reply

HI = {"messages": [{"role": "user", "content": "hi"}]}


# --- the limiter on its own ---------------------------------------------------


def test_allows_up_to_capacity_then_blocks() -> None:
    limiter = RateLimiter(per_minute=3, clock=FakeClock())

    assert [limiter.check("1.1.1.1") for _ in range(3)] == [0, 0, 0]
    assert limiter.check("1.1.1.1") == pytest.approx(20.0)


def test_tokens_refill_over_time() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_minute=6, clock=clock)  # one token every 10 s
    for _ in range(6):
        limiter.check("a")
    assert limiter.check("a") > 0

    clock.advance(10)
    assert limiter.check("a") == 0
    assert limiter.check("a") > 0

    clock.advance(600)  # refill caps at capacity
    assert [limiter.check("a") for _ in range(7)].count(0) == 6


def test_blocked_requests_do_not_extend_the_wait() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_minute=1, clock=clock)
    limiter.check("a")

    first_wait = limiter.check("a")
    clock.advance(30)
    assert limiter.check("a") == pytest.approx(first_wait - 30)


def test_separate_ips_have_separate_buckets() -> None:
    limiter = RateLimiter(per_minute=1, clock=FakeClock())

    assert limiter.check("1.1.1.1") == 0
    assert limiter.check("1.1.1.1") > 0
    assert limiter.check("2.2.2.2") == 0


def test_least_recently_seen_ip_is_evicted() -> None:
    limiter = RateLimiter(per_minute=1, max_clients=2, clock=FakeClock())
    limiter.check("a")
    limiter.check("b")
    limiter.check("a")  # "a" is now the most recently seen
    limiter.check("c")  # evicts "b"

    assert limiter.tracked_clients() == 2
    assert limiter.check("b") == 0  # forgotten, so it starts with a full bucket
    assert limiter.check("c") > 0  # still tracked, bucket still empty


def test_tracked_clients_never_exceed_the_cap() -> None:
    limiter = RateLimiter(per_minute=10, max_clients=1000, clock=FakeClock())

    for i in range(1500):
        limiter.check(f"10.0.{i // 256}.{i % 256}")

    assert limiter.tracked_clients() == 1000


@pytest.mark.parametrize(("seconds", "header"), [(0.2, "1"), (5.0, "5"), (5.1, "6")])
def test_retry_after_is_whole_seconds_rounded_up(seconds: float, header: str) -> None:
    assert retry_after_header(seconds) == header


# --- wired into the API -------------------------------------------------------


def app_client(per_minute: int, clock: FakeClock | None = None) -> TestClient:
    limiter = RateLimiter(per_minute=per_minute, clock=clock or FakeClock())
    fake = FakeGroq(*[reply("Hello!") for _ in range(10)])
    app = create_app(
        settings=Settings(groq_api_key="test-key"),
        client=fake,
        store=Store.fresh(),
        limiter=limiter,
    )
    return TestClient(app)


def post(client: TestClient, ip: str | None = None, body: dict[str, Any] = HI) -> Any:
    headers = {"X-Forwarded-For": ip} if ip else {}
    return client.post("/chat", json=body, headers=headers)


def test_chat_returns_429_with_retry_after() -> None:
    client = app_client(per_minute=2)

    assert [post(client).status_code for _ in range(2)] == [200, 200]
    blocked = post(client)

    assert blocked.status_code == 429
    assert blocked.json() == {"detail": RATE_LIMITED}
    assert blocked.headers["Retry-After"] == "30"


def test_chat_allows_again_after_refill() -> None:
    clock = FakeClock()
    client = app_client(per_minute=1, clock=clock)
    assert post(client).status_code == 200
    assert post(client).status_code == 429

    clock.advance(60)

    assert post(client).status_code == 200


def test_first_forwarded_for_address_is_the_client() -> None:
    client = app_client(per_minute=1)

    assert post(client, ip="203.0.113.5, 10.0.0.1").status_code == 200
    assert post(client, ip="203.0.113.5, 10.0.0.2").status_code == 429
    assert post(client, ip="198.51.100.7, 10.0.0.1").status_code == 200


def test_invalid_requests_also_count() -> None:
    client = app_client(per_minute=1)

    assert post(client, body={"messages": []}).status_code == 422
    assert post(client).status_code == 429


def test_health_is_not_rate_limited() -> None:
    client = app_client(per_minute=1)
    post(client)
    post(client)

    assert all(client.get("/health").status_code == 200 for _ in range(20))
