"""A scripted stand-in for the Groq client, so tests never call the real API."""

import json
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


def rate_limit_error() -> Exception:
    response = httpx.Response(429, request=_REQUEST)
    return groq.RateLimitError("rate limited", response=response, body=None)


class FakeGroq:
    """Returns (or raises) the scripted items in order and records every request."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        # Copy the messages: the loop keeps appending to the same list after the call.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self.script:
            raise AssertionError("FakeGroq ran out of scripted responses")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
