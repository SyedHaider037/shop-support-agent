"""One model call with our own retry policy and a hard deadline.

The SDK's built-in retries are off (max_retries=0) because they wait out
Groq's retry-after without knowing about our request deadline. Here every
wait is checked against the time left first, so a request never runs past
its deadline, and a busy model is reported as busy instead of hanging.
"""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import groq

from app.config import Settings

logger = logging.getLogger(__name__)

# Used when Groq sends a 429 without a usable retry-after header.
DEFAULT_RETRY_AFTER_S = 2.0
# Backoff before retrying a 5xx: 1 s, then 2 s, then 4 s ...
SERVER_ERROR_BACKOFF_S = 1.0

RetryReason = Literal["rate_limited", "server_error", "tool_use_failed"]


@dataclass(frozen=True)
class RetryRecord:
    reason: RetryReason
    wait_s: float


@dataclass
class CallOutcome:
    response: Any = None
    error: str | None = None
    # Groq's retry-after when the call ended as model_busy, so callers can pass it on.
    retry_after_s: float | None = None


def retry_after_seconds(exc: groq.APIStatusError) -> float:
    raw = exc.response.headers.get("retry-after", "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_RETRY_AFTER_S
    return value if math.isfinite(value) and value >= 0 else DEFAULT_RETRY_AFTER_S


def is_tool_use_failed(exc: groq.BadRequestError) -> bool:
    """Groq returns 400 tool_use_failed when the model emits a malformed tool call."""
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error", body)
        return isinstance(error, dict) and error.get("code") == "tool_use_failed"
    return False


def call_model(
    client: groq.Groq,
    request: dict[str, Any],
    settings: Settings,
    deadline: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    retries: list[RetryRecord],
) -> CallOutcome:
    """Call the model, retrying 429/5xx up to MAX_RETRIES and tool_use_failed once."""
    status_retries = 0
    tool_use_retried = False

    def fits(wait: float) -> bool:
        # Only wait if a full-length call still fits after the wait.
        return wait + settings.request_timeout_s <= deadline - clock()

    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return CallOutcome(error="deadline")
        try:
            response = client.chat.completions.create(
                **request, timeout=min(settings.request_timeout_s, remaining)
            )
            return CallOutcome(response=response)
        except groq.APITimeoutError:
            return CallOutcome(error="deadline" if clock() >= deadline else "timeout")
        except groq.RateLimitError as exc:
            wait = retry_after_seconds(exc)
            if status_retries >= settings.max_retries or not fits(wait):
                return CallOutcome(error="model_busy", retry_after_s=wait)
            reason: RetryReason = "rate_limited"
        except groq.InternalServerError:
            wait = SERVER_ERROR_BACKOFF_S * 2**status_retries
            if status_retries >= settings.max_retries or not fits(wait):
                return CallOutcome(error="api_error")
            reason = "server_error"
        except groq.BadRequestError as exc:
            if not is_tool_use_failed(exc):
                logger.warning("Groq rejected the request: 400")
                return CallOutcome(error="api_error")
            if tool_use_retried:
                return CallOutcome(error="tool_call_failed")
            tool_use_retried = True
            retries.append(RetryRecord(reason="tool_use_failed", wait_s=0.0))
            continue
        except groq.APIError as exc:
            logger.warning("Groq API error: %s", type(exc).__name__)
            return CallOutcome(error="api_error")

        status_retries += 1
        retries.append(RetryRecord(reason=reason, wait_s=wait))
        sleep(wait)
