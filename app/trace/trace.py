"""One trace per /chat request: what the agent did, how long it took, what it cost.

The trace goes back to the caller (it is their own conversation) and, without
arguments, into one JSON log line. Logs must never hold names, emails or
message text, so the log copy keeps only argument names, never values.
"""

import json
from collections.abc import Mapping
from typing import Any

from app.agent.loop import AgentResult, Step
from app.config import MODEL_PRICES_PER_MILLION, Settings
from app.tools.definitions import TOOLS


def estimated_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    prices: Mapping[str, tuple[float, float]] = MODEL_PRICES_PER_MILLION,
) -> float | None:
    """Estimated USD cost from list prices, or None when the model's price is unknown."""
    price = prices.get(model)
    if price is None:
        return None
    input_per_million, output_per_million = price
    cost = (prompt_tokens * input_per_million + completion_tokens * output_per_million) / 1e6
    return round(cost, 8)


def _tool_call(step: Step) -> dict[str, Any]:
    spec = TOOLS.get(step.tool)
    return {
        "name": step.tool,
        "risk": spec.risk if spec else "unknown",
        "arguments": step.arguments,  # already email-masked by the loop
        "result": "ok" if step.result.get("ok") else step.result.get("error", "error"),
        "latency_ms": step.latency_ms,
    }


def build_trace(
    result: AgentResult,
    settings: Settings,
    request_id: str,
    total_latency_ms: int,
    prices: Mapping[str, tuple[float, float]] = MODEL_PRICES_PER_MILLION,
) -> dict[str, Any]:
    usage = result.usage
    return {
        "request_id": request_id,
        "model": settings.model,
        "reasoning_effort": settings.reasoning_effort if settings.uses_reasoning_effort else None,
        "steps": [
            {
                "step": step.step,
                "latency_ms": step.latency_ms,
                "prompt_tokens": step.prompt_tokens,
                "completion_tokens": step.completion_tokens,
                "tool_calls": [_tool_call(call) for call in step.tool_calls],
            }
            for step in result.model_steps
        ],
        "guardrail_events": [
            {"layer": e.layer, "rule": e.rule, "action": e.action} for e in result.guardrail_events
        ],
        "retries": [{"reason": r.reason, "wait_s": r.wait_s} for r in result.retries],
        "total_latency_ms": total_latency_ms,
        "tokens": {
            "prompt": usage.prompt_tokens,
            "completion": usage.completion_tokens,
            "total": usage.total_tokens,
        },
        "estimated_cost_usd": estimated_cost_usd(
            settings.model, usage.prompt_tokens, usage.completion_tokens, prices
        ),
        "error": result.error,
    }


def log_line(trace: dict[str, Any]) -> str:
    """The trace as one JSON line, with argument values dropped (only their names kept)."""
    steps = [
        {
            **step,
            "tool_calls": [
                {
                    **call,
                    "arguments": sorted(call["arguments"])
                    if isinstance(call["arguments"], dict)
                    else "<unparsed>",
                }
                for call in step["tool_calls"]
            ],
        }
        for step in trace["steps"]
    ]
    return json.dumps({"event": "chat_trace", **trace, "steps": steps}, separators=(",", ":"))
