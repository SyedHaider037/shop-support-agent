"""Keep eval traffic under Groq's per-minute limits instead of hitting them.

Groq allows 8,000 tokens and 30 requests per minute per model on this plan.
Waiting for a 429 wastes a request and its retry-after can be long, so every
call is delayed until a sliding 60 s window has room for it. Budgets sit
below the hard limits to leave room for estimation error.

One Pacer per model: the limits are per model.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

WINDOW_S = 60.0
TPM_BUDGET = 6_500
RPM_BUDGET = 25
UNKNOWN_ESTIMATE = 2_000
ESTIMATE_MARGIN = 400


@dataclass
class PacerStats:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    requests: int = 0
    waited_s: float = 0.0


def _cached_tokens(usage: Any) -> int:
    details = getattr(usage, "prompt_tokens_details", None)
    return getattr(details, "cached_tokens", 0) or 0


class Pacer:
    """Wraps a Groq client; exposes the same chat.completions.create used by the agent."""

    def __init__(
        self,
        client: Any,
        tpm_budget: int = TPM_BUDGET,
        rpm_budget: int = RPM_BUDGET,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self.tpm_budget = tpm_budget
        self.rpm_budget = rpm_budget
        self._clock = clock
        self._sleep = sleep
        self._tokens: deque[tuple[float, int]] = deque()  # (time, tokens that count)
        self._requests: deque[float] = deque()
        self._last_prompt: int | None = None
        self.stats = PacerStats()  # running totals; callers may reset between tasks
        self.total_waited_s = 0.0  # never reset: the runner excludes it from deadlines
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def estimate(self) -> int:
        """Tokens the next call will probably use: last prompt plus room for the reply."""
        if self._last_prompt is None:
            return UNKNOWN_ESTIMATE
        return self._last_prompt + ESTIMATE_MARGIN

    def _prune(self, now: float) -> None:
        while self._tokens and self._tokens[0][0] <= now - WINDOW_S:
            self._tokens.popleft()
        while self._requests and self._requests[0] <= now - WINDOW_S:
            self._requests.popleft()

    def _fits(self, estimate: int) -> bool:
        used = sum(tokens for _, tokens in self._tokens)
        # A call bigger than the whole budget still goes once the window is empty.
        tokens_ok = used + estimate <= self.tpm_budget or not self._tokens
        return tokens_ok and len(self._requests) + 1 <= self.rpm_budget

    def wait_for_room(self) -> float:
        """Sleep until the next call fits in the window; returns the seconds waited."""
        estimate = self.estimate()
        waited = 0.0
        while True:
            now = self._clock()
            self._prune(now)
            if self._fits(estimate):
                break
            # Sleep until the oldest entry leaves the window, then check again.
            starts = [self._tokens[0][0]] if self._tokens else []
            starts += [self._requests[0]] if self._requests else []
            pause = max(min(starts) + WINDOW_S - now, 0.0) + 0.05
            self._sleep(pause)
            waited += pause
        self.stats.waited_s += waited
        self.total_waited_s += waited
        return waited

    def _create(self, **kwargs: Any) -> Any:
        self.wait_for_room()
        self._requests.append(self._clock())
        self.stats.requests += 1
        response = self._client.chat.completions.create(**kwargs)  # errors propagate
        usage = getattr(response, "usage", None)
        if usage is not None:
            prompt = usage.prompt_tokens or 0
            cached = _cached_tokens(usage)
            completion = usage.completion_tokens or 0
            self._last_prompt = prompt
            # Cached prompt tokens do not count against Groq's limits.
            self._tokens.append((self._clock(), prompt - cached + completion))
            self.stats.prompt_tokens += prompt
            self.stats.completion_tokens += completion
            self.stats.cached_tokens += cached
        return response

    def take_stats(self) -> PacerStats:
        """Return the totals since the last call and start counting again."""
        stats, self.stats = self.stats, PacerStats()
        return stats
