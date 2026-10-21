"""Run a tool call from the model and always return a result, never raise.

Model output is untrusted: bad JSON, unknown tools and invalid arguments are
reported back as error results so the model can correct itself, and a tool
bug cannot take down the request.
"""

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from app.store.store import Store
from app.tools import handlers
from app.tools.consent import has_consent
from app.tools.definitions import TOOLS

logger = logging.getLogger(__name__)

ToolResult = dict[str, Any]
Handler = Callable[[Any, Store], dict[str, Any]]

HANDLERS: dict[str, Handler] = {
    "search_products": handlers.search_products,
    "get_order_status": handlers.get_order_status,
    "capture_lead": handlers.capture_lead,
    "request_refund": handlers.request_refund,
}


def _error(code: str, detail: list[str] | None = None) -> ToolResult:
    result: ToolResult = {"ok": False, "error": code}
    if detail:
        result["detail"] = detail
    return result


def _validation_detail(exc: ValidationError) -> list[str]:
    """Field names and messages only; never echo the rejected input values."""
    return [
        f"{'.'.join(str(part) for part in err['loc']) or 'arguments'}: {err['msg']}"
        for err in exc.errors()
    ]


# Tools that save a customer's personal details: they need the customer's own consent.
NEEDS_CONSENT = frozenset({"capture_lead"})


def dispatch(
    tool_name: str,
    raw_arguments: str | None,
    store: Store,
    conversation: Sequence[Mapping[str, str]] = (),
) -> ToolResult:
    """Run one tool call. `conversation` is the chat so far; without a customer message that
    agrees, a tool in NEEDS_CONSENT is refused with consent_required."""
    spec = TOOLS.get(tool_name)
    handler = HANDLERS.get(tool_name)
    if spec is None or handler is None:
        return _error("unknown_tool")

    try:
        parsed = json.loads(raw_arguments) if raw_arguments and raw_arguments.strip() else {}
    except json.JSONDecodeError:
        return _error("invalid_json")
    if not isinstance(parsed, dict):
        return _error("invalid_arguments", ["arguments: must be a JSON object"])

    try:
        args: BaseModel = spec.args_model.model_validate(parsed)
    except ValidationError as exc:
        return _error("invalid_arguments", _validation_detail(exc))
    if tool_name in NEEDS_CONSENT and not has_consent(conversation):
        return _error("consent_required")

    try:
        return {"ok": True, "data": handler(args, store)}
    except handlers.ToolError as exc:
        return _error(exc.code)
    except Exception:
        logger.exception("tool %s failed", tool_name)
        return _error("tool_failed")
