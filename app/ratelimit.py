"""Per-visitor rate limiting with a token bucket.

Every model call costs money, so one visitor must not be able to drain the
Groq quota. Each IP gets a bucket of `per_minute` tokens that refills
continuously; a request spends one token.

This is best-effort only. On serverless hosting each instance has its own
memory, so a visitor spread across several instances gets more than the
limit, and the counts reset on every cold start. A shared store (e.g. Redis)
would be needed for a hard limit.
"""

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from fastapi import Request


class RateLimiter:
    def __init__(
        self,
        per_minute: int,
        max_clients: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = float(per_minute)
        self.refill_per_s = per_minute / 60.0
        self.max_clients = max_clients
        self._clock = clock
        # client -> (tokens left, last update). Ordered by last use for eviction.
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        # FastAPI runs sync endpoints in a thread pool, so updates must be atomic.
        self._lock = threading.Lock()

    def check(self, client: str) -> float:
        """Spend one token. Returns 0 if allowed, else the seconds until a token is free."""
        with self._lock:
            now = self._clock()
            tokens, updated = self._buckets.pop(client, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - updated) * self.refill_per_s)
            allowed = tokens >= 1
            if allowed:
                tokens -= 1
            self._buckets[client] = (tokens, now)
            # Bound memory: forget the least recently seen visitors first.
            while len(self._buckets) > self.max_clients:
                self._buckets.popitem(last=False)
            return 0.0 if allowed else (1 - tokens) / self.refill_per_s

    def tracked_clients(self) -> int:
        return len(self._buckets)


def client_ip(request: Request) -> str:
    """The visitor's IP: the first X-Forwarded-For hop when behind a proxy, else the peer.

    X-Forwarded-For is only trustworthy when a proxy we control sets it (Vercel
    does); a client calling the app directly could put anything there.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"


def retry_after_header(seconds: float) -> str:
    return str(max(1, math.ceil(seconds)))
