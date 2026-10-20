"""The agent loop: call the model, run the tools it asks for, repeat until it answers.

The Groq client is passed in so tests can script the model, and every API
failure becomes an AgentResult with a safe reply instead of an exception.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import groq

from app.agent.policy import system_prompt
from app.config import Settings
from app.guardrails.input import check_input
from app.guardrails.rules import GuardrailEvent, GuardrailRules, guardrail_rules
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
    guardrail_events: list[GuardrailEvent] = field(default_factory=list)
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
    messages: list[dict[str, str]],
    store: Store,
    client: groq.Groq,
    settings: Settings,
    clock: Callable[[], float] = time.monotonic,
    rules: GuardrailRules | None = None,
) -> AgentResult:
    """Answer the last user message, given the full conversation so far.

    The clock and rules are injectable so tests and evals control them.
    """
    deadline = clock() + settings.request_deadline_s
    rules = rules or guardrail_rules()
    result = AgentResult(reply=HANDOFF_REPLY)

    checked = check_input(messages, rules)
    if checked.event is not None:
        result.guardrail_events.append(checked.event)
        result.reply = rules.input_refusal
        result.error = "input_refused"
        return result

    conversation: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt()},
        *checked.messages,
    ]
    tools = tool_schemas()
    extra: dict[str, Any] = {}
    if settings.uses_reasoning_effort:
        extra["reasoning_effort"] = settings.reasoning_effort
    force_tool = False
    retried_fake_check = False

    for _ in range(settings.max_steps):
        remaining = deadline - clock()
        if remaining <= 0:
            return _stopped(result, "deadline")
        try:
            response = client.chat.completions.create(
                model=settings.model,
                messages=conversation,
                tools=tools,
                tool_choice="required" if force_tool else "auto",
                temperature=settings.temperature,
                timeout=min(settings.request_timeout_s, remaining),
                **extra,
            )
        except groq.APITimeoutError:
            # A timeout cut short by the request deadline is the deadline, not an outage.
            return (
                _stopped(result, "deadline") if clock() >= deadline else _failed(result, "timeout")
            )
        except groq.RateLimitError:
            return _failed(result, "rate_limited")
        except groq.APIError as exc:
            logger.warning("Groq API error: %s", type(exc).__name__)
            return _failed(result, "api_error")

        _add_usage(result.usage, response)
        over_budget = result.usage.total_tokens > settings.token_budget
        message = response.choices[0].message
        force_tool = False

        if not message.tool_calls:
            content = (message.content or "").strip()
            if not over_budget and not retried_fake_check and sounds_like_fake_check(content):
                logger.info("reply narrated a check without a tool call; retrying with a tool")
                retried_fake_check = True
                force_tool = True
                continue
            if not content:
                return _stopped(result, "token_budget" if over_budget else "empty_reply")
            # A finished answer is kept even if it crossed the budget: the tokens are spent.
            result.reply = content
            return result

        if over_budget:
            # Stop before running more tools: that would only lead to another model call.
            return _stopped(result, "token_budget")

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

    return _stopped(result, "max_steps")


def _stopped(result: AgentResult, code: str) -> AgentResult:
    """A limit was hit: hand off to a human rather than guess."""
    result.reply = HANDOFF_REPLY
    result.error = code
    return result


def _failed(result: AgentResult, code: str) -> AgentResult:
    result.reply = API_ERROR_REPLY
    result.error = code
    return result
