"""The agent loop: call the model, run the tools it asks for, repeat until it answers.

The Groq client is passed in so tests can script the model, and every API
failure becomes an AgentResult with a safe reply instead of an exception.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import groq

from app.agent.policy import system_prompt
from app.config import Settings
from app.store.store import Store
from app.tools.definitions import tool_schemas
from app.tools.dispatcher import dispatch
from app.trace.redact import mask_emails

logger = logging.getLogger(__name__)

HANDOFF_REPLY = (
    "Sorry, I couldn't finish that request. A member of our support team will follow up with you."
)
API_ERROR_REPLY = "Sorry, our assistant is having trouble right now. Please try again in a moment."

# Errors that mean the model could not be reached; callers report these as unavailable.
API_ERRORS = frozenset({"timeout", "rate_limited", "api_error"})

# Models sometimes say "let me check" in plain text without calling a tool, then
# guess. Seeing one of these with no tool call means: retry once, forcing a tool call.
FAKE_CHECK_PHRASES = (
    "let me check",
    "let me look",
    "give me a moment",
    "just a moment",
    "one moment",
    "checking on",
    "checking the",
    "i've checked",
    "i have checked",
    "after checking",
    "look up the",
    "looking up",
    "looking into",
)


@dataclass
class Step:
    tool: str
    arguments: Any
    result: dict[str, Any]
    latency_ms: int


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0


@dataclass
class AgentResult:
    reply: str
    steps: list[Step] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    error: str | None = None


def sounds_like_fake_check(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in FAKE_CHECK_PHRASES)


def _record_arguments(raw: str | None) -> Any:
    """A masked copy of the arguments for the trace; unparseable input is kept as masked text."""
    try:
        return mask_emails(json.loads(raw or "{}"))
    except json.JSONDecodeError:
        return mask_emails(raw or "")


def _add_usage(usage: Usage, response: Any) -> None:
    usage.model_calls += 1
    reported = getattr(response, "usage", None)
    if reported is not None:
        usage.prompt_tokens += reported.prompt_tokens or 0
        usage.completion_tokens += reported.completion_tokens or 0
        usage.total_tokens += reported.total_tokens or 0


def _assistant_turn(message: Any) -> dict[str, Any]:
    """Rebuild the assistant message by hand: echoing the SDK object back can include
    response-only fields the API rejects on input."""
    turn: dict[str, Any] = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in message.tool_calls
        ],
    }
    if message.content:
        turn["content"] = message.content
    return turn


def run_agent(
    messages: list[dict[str, str]], store: Store, client: groq.Groq, settings: Settings
) -> AgentResult:
    """Answer the last user message, given the full conversation so far."""
    conversation: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt()},
        *({"role": m["role"], "content": m["content"]} for m in messages),
    ]
    result = AgentResult(reply=HANDOFF_REPLY)
    tools = tool_schemas()
    force_tool = False
    retried_fake_check = False

    for _ in range(settings.max_steps):
        try:
            response = client.chat.completions.create(
                model=settings.model,
                messages=conversation,
                tools=tools,
                tool_choice="required" if force_tool else "auto",
                temperature=settings.temperature,
            )
        except groq.APITimeoutError:
            return _failed(result, "timeout")
        except groq.RateLimitError:
            return _failed(result, "rate_limited")
        except groq.APIError as exc:
            logger.warning("Groq API error: %s", type(exc).__name__)
            return _failed(result, "api_error")

        _add_usage(result.usage, response)
        message = response.choices[0].message
        force_tool = False

        if not message.tool_calls:
            content = (message.content or "").strip()
            if not retried_fake_check and sounds_like_fake_check(content):
                logger.info("reply narrated a check without a tool call; retrying with a tool")
                retried_fake_check = True
                force_tool = True
                continue
            if not content:
                result.error = "empty_reply"
                return result
            result.reply = content
            return result

        conversation.append(_assistant_turn(message))
        for call in message.tool_calls:
            started = time.perf_counter()
            tool_result = dispatch(call.function.name, call.function.arguments, store)
            latency_ms = round((time.perf_counter() - started) * 1000)
            result.steps.append(
                Step(
                    tool=call.function.name,
                    arguments=_record_arguments(call.function.arguments),
                    result=mask_emails(tool_result),
                    latency_ms=latency_ms,
                )
            )
            conversation.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(tool_result)}
            )

    result.error = "max_steps"
    return result


def _failed(result: AgentResult, code: str) -> AgentResult:
    result.reply = API_ERROR_REPLY
    result.error = code
    return result
