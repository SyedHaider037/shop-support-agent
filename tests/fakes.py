"""A scripted stand-in for the Groq client, so tests never call the real API."""

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import groq
import httpx

_REQUEST = httpx.Request("POST", "https://api.groq.test/openai/v1/chat/completions")


def reply(text: str | None, tokens: int = 10) -> SimpleNamespace:
    return _response(SimpleNamespace(content=text, tool_calls=None), tokens)


def tool_call(
    name: str, arguments: dict[str, Any] | str, call_id: str = "call_1"
) -> SimpleNamespace:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=raw))


def tool_calls(*calls: SimpleNamespace, tokens: int = 10) -> SimpleNamespace:
    return _response(SimpleNamespace(content=None, tool_calls=list(calls)), tokens)


def _response(message: SimpleNamespace, tokens: int) -> SimpleNamespace:
    usage = SimpleNamespace(prompt_tokens=tokens, completion_tokens=1, total_tokens=tokens + 1)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def connection_error() -> Exception:
    return groq.APIConnectionError(request=_REQUEST)


def timeout_error() -> Exception:
    return groq.APITimeoutError(request=_REQUEST)


def rate_limit_error(retry_after: str | None = "30") -> groq.RateLimitError:
    """A Groq 429. The default retry-after is too long to wait, so nothing sleeps by accident."""
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(429, request=_REQUEST, headers=headers)
    return groq.RateLimitError("rate limited", response=response, body=None)


def server_error(status: int = 503) -> Exception:
    response = httpx.Response(status, request=_REQUEST)
    return groq.InternalServerError("server error", response=response, body=None)


def tool_use_failed_error() -> Exception:
    response = httpx.Response(400, request=_REQUEST)
    body = {"error": {"code": "tool_use_failed", "message": "Failed to call a function"}}
    return groq.BadRequestError("tool_use_failed", response=response, body=body)


def bad_request_error() -> Exception:
    response = httpx.Response(400, request=_REQUEST)
    body = {"error": {"code": "invalid_request", "message": "bad"}}
    return groq.BadRequestError("bad request", response=response, body=body)


class FakeClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeGroq:
    """Returns (or raises) the scripted items in order and records every request.

    on_call runs before each response, e.g. to advance a FakeClock.
    """

    def __init__(self, *script: Any, on_call: Callable[[], None] | None = None) -> None:
        self.script = list(script)
        self.on_call = on_call
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        # Copy the messages: the loop keeps appending to the same list after the call.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self.script:
            raise AssertionError("FakeGroq ran out of scripted responses")
        item = self.script.pop(0)
        if self.on_call is not None:
            self.on_call()
        if isinstance(item, Exception):
            raise item
        return item
