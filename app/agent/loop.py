"""The agent loop: call the model, run the tools it asks for, repeat until it answers.

The Groq client is passed in so tests can script the model, and every API
failure becomes an AgentResult with a safe reply instead of an exception.
"""

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import groq

from app.agent.model_call import RetryRecord, call_model
from app.agent.policy import system_prompt
from app.config import Settings
from app.guardrails.input import check_input
from app.guardrails.output import check_output
from app.guardrails.rules import GuardrailEvent, GuardrailRules, guardrail_rules
from app.store.store import Store
from app.tools.definitions import TOOLS, tool_schemas
from app.tools.dispatcher import dispatch
from app.trace.redact import mask_emails

logger = logging.getLogger(__name__)

HANDOFF_REPLY = (
    "Sorry, I couldn't finish that request. A member of our support team will follow up with you."
)
API_ERROR_REPLY = "Sorry, our assistant is having trouble right now. Please try again in a moment."
BUSY_REPLY = "We're busy right now, please try again in a minute."

# The model could not be reached at all; callers report these as an outage.
API_ERRORS = frozenset({"timeout", "api_error"})
# The model is up but over its rate limit; callers report this as busy, not broken.
BUSY_ERROR = "model_busy"

# Models sometimes say "let me check" in plain text without calling a tool, then
# guess. Seeing one of these with no tool call can mean: retry once, forcing a tool call.
FAKE_CHECK_PHRASES = (
    "let me check",
    "let me look",
    "i'll check",
    "i will check",
    "i'll look",
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
# A reply that asks the customer for something is a finished turn, even if it also says
# "I can look up the order". Forcing a tool call then makes Groq reject the call (400
# tool_use_failed) and the customer gets the handoff text instead of the question.
ASKING_PHRASES = (
    "?",
    "could you",
    "can you",
    "would you",
    "please provide",
    "please share",
    "please send",
    "please confirm",
    "please let me know",
    "may i have",
    "what is your",
)
_ORDER_ID = re.compile(r"\bORD[-‑]?\d+\b", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PRODUCT_WORDS = re.compile(r"\b(?:product|products|stock|catalog|price|prices|item|items)\b", re.I)


@dataclass
class Step:
    tool: str
    arguments: Any
    result: dict[str, Any]
    latency_ms: int


@dataclass
class ModelStep:
    """One successful model call and the tools it asked for, for the trace."""

    step: int
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    tool_calls: list[Step] = field(default_factory=list)


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
    model_steps: list[ModelStep] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    guardrail_events: list[GuardrailEvent] = field(default_factory=list)
    retries: list[RetryRecord] = field(default_factory=list)
    error: str | None = None
    # Set with model_busy: how long Groq asked us to wait.
    retry_after_s: float | None = None
    # Why a tool call was forced ("promised_lookup"), or None if it never was.
    forced_retry_reason: str | None = None
    # The reply that triggered the forced retry. Raw model text that never passed the
    # output guardrails, so it is for evals only: never put it in the API or the logs.
    first_reply: str | None = None
    # An error the loop recovered from, e.g. tool_call_failed after a forced retry.
    recovered_error: str | None = None
    # The model's final text before the output guardrails changed it, so evals can tell a
    # real leak from a false alarm. It may hold leaked text: never in the API or the logs.
    reply_before_guardrails: str | None = None


def sounds_like_fake_check(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in FAKE_CHECK_PHRASES)


def asks_customer(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in ASKING_PHRASES)


def lookup_args_available(user_texts: list[str], reply: str) -> bool:
    """Could a forced tool call be valid? Orders need an ID and an email from the customer;
    a product search only needs words, so any talk of products qualifies."""
    typed = "\n".join(user_texts)
    if _ORDER_ID.search(typed) and _EMAIL.search(typed):
        return True
    return bool(_PRODUCT_WORDS.search(reply) or _PRODUCT_WORDS.search(typed))


def should_force_tool(reply: str, user_texts: list[str]) -> bool:
    """Force a tool call only for a promise to look something up that the model could keep:
    no question to the customer, and the arguments the tool needs are available."""
    return (
        sounds_like_fake_check(reply)
        and not asks_customer(reply)
        and lookup_args_available(user_texts, reply)
    )


def write_succeeded(steps: list[Step]) -> bool:
    """A write tool (refund request, lead) returned ok in this request."""
    return any(
        (spec := TOOLS.get(s.tool)) is not None and not spec.read_only and s.result.get("ok")
        for s in steps
    )


def _record_arguments(raw: str | None) -> Any:
    """A masked copy of the arguments for the trace; unparseable input is kept as masked text."""
    try:
        return mask_emails(json.loads(raw or "{}"))
    except json.JSONDecodeError:
        return mask_emails(raw or "")


def _add_usage(usage: Usage, response: Any) -> tuple[int, int]:
    """Add one call's tokens to the totals; returns that call's (prompt, completion) tokens."""
    usage.model_calls += 1
    reported = getattr(response, "usage", None)
    if reported is None:
        return 0, 0
    prompt, completion = reported.prompt_tokens or 0, reported.completion_tokens or 0
    usage.prompt_tokens += prompt
    usage.completion_tokens += completion
    usage.total_tokens += reported.total_tokens or 0
    return prompt, completion


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
    sleep: Callable[[float], None] = time.sleep,
) -> AgentResult:
    """Answer the last user message, given the full conversation so far.

    The clock, sleep and rules are injectable so tests and evals control them.
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
    user_texts = [m["content"] for m in checked.messages if m["role"] == "user"]
    force_tool = False
    retried_fake_check = False

    for _ in range(settings.max_steps):
        request = {
            "model": settings.model,
            "messages": conversation,
            "tools": tools,
            "tool_choice": "required" if force_tool else "auto",
            "temperature": settings.temperature,
            **extra,
        }
        started = clock()
        outcome = call_model(client, request, settings, deadline, clock, sleep, result.retries)
        if outcome.error == "tool_call_failed" and force_tool and result.first_reply:
            # The forced call failed, but the model's own reply is still there: send it
            # (if the output checks allow it) rather than a generic handoff.
            result.reply_before_guardrails = result.first_reply
            recovered = check_output(
                result.first_reply, user_texts, rules, write_succeeded=write_succeeded(result.steps)
            )
            if recovered.events:
                return _call_failed(result, outcome.error, outcome.retry_after_s)
            result.reply = recovered.reply
            result.recovered_error = outcome.error
            return result
        if outcome.error is not None:
            return _call_failed(result, outcome.error, outcome.retry_after_s)
        response = outcome.response

        prompt_tokens, completion_tokens = _add_usage(result.usage, response)
        model_step = ModelStep(
            step=len(result.model_steps) + 1,
            # Includes any retries and waits inside call_model.
            latency_ms=round((clock() - started) * 1000),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        result.model_steps.append(model_step)
        over_budget = result.usage.total_tokens > settings.token_budget
        message = response.choices[0].message
        force_tool = False

        if not message.tool_calls:
            content = (message.content or "").strip()
            if (
                not over_budget
                and not retried_fake_check
                and should_force_tool(content, user_texts)
            ):
                logger.info("reply promised a lookup without a tool call; retrying with a tool")
                retried_fake_check = True
                force_tool = True
                result.forced_retry_reason = "promised_lookup"
                result.first_reply = content
                continue
            if not content:
                return _stopped(result, "token_budget" if over_budget else "empty_reply")
            # A finished answer is kept even if it crossed the budget: the tokens are spent.
            result.reply_before_guardrails = content
            checked_reply = check_output(
                content, user_texts, rules, write_succeeded=write_succeeded(result.steps)
            )
            result.reply = checked_reply.reply
            result.guardrail_events.extend(checked_reply.events)
            return result

        if over_budget:
            # Stop before running more tools: that would only lead to another model call.
            return _stopped(result, "token_budget")

        conversation.append(_assistant_turn(message))
        for call in message.tool_calls:
            tool_started = time.perf_counter()  # real time, not the injectable request clock
            tool_result = dispatch(call.function.name, call.function.arguments, store)
            latency_ms = round((time.perf_counter() - tool_started) * 1000)
            step = Step(
                tool=call.function.name,
                arguments=_record_arguments(call.function.arguments),
                result=mask_emails(tool_result),
                latency_ms=latency_ms,
            )
            result.steps.append(step)
            model_step.tool_calls.append(step)
            conversation.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(tool_result)}
            )

    return _stopped(result, "max_steps")


def _stopped(result: AgentResult, code: str) -> AgentResult:
    """A limit was hit: hand off to a human rather than guess."""
    result.reply = HANDOFF_REPLY
    result.error = code
    return result


def _call_failed(result: AgentResult, code: str, retry_after_s: float | None) -> AgentResult:
    if code == BUSY_ERROR:
        result.reply = BUSY_REPLY
        result.error = code
        result.retry_after_s = retry_after_s
        return result
    if code in API_ERRORS:
        result.reply = API_ERROR_REPLY
        result.error = code
        return result
    # deadline or tool_call_failed: the model was reachable, we just could not finish.
    return _stopped(result, code)
