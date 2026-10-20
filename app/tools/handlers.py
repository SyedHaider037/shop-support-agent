"""Tool implementations. Each returns plain data or raises ToolError with a code."""

from typing import Any

from app.store.models import Order
from app.store.store import Store
from app.tools.definitions import (
    CaptureLeadArgs,
    GetOrderStatusArgs,
    RequestRefundArgs,
    SearchProductsArgs,
)
from app.tools.refund_rules import ineligibility


class ToolError(Exception):
    """A failure the model should see as an error code, not a crash."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def search_products(args: SearchProductsArgs, store: Store) -> dict[str, Any]:
    products = store.search_products(args.query, args.max_results)
    return {"products": [p.model_dump(mode="json") for p in products]}


def _verified_order(order_id: str, email: str, store: Store) -> Order:
    """The order, if the email matches it.

    An unknown order and a wrong email raise the same error, so no tool can be
    used to discover which order IDs exist.
    """
    order = store.get_order(order_id)
    if order is None or order.email.lower() != email.strip().lower():
        raise ToolError("no_matching_order")
    return order


def get_order_status(args: GetOrderStatusArgs, store: Store) -> dict[str, Any]:
    order = _verified_order(args.order_id, args.email, store)
    return {
        "order_id": order.order_id,
        "status": order.status,
        "items": [item.name for item in order.items],
        "order_date": order.order_date.isoformat(),
        "shipped_date": order.shipped_date.isoformat() if order.shipped_date else None,
        "delivered_date": order.delivered_date.isoformat() if order.delivered_date else None,
        "tracking_number": order.tracking_number,
        "total": order.total,
    }


def request_refund(args: RequestRefundArgs, store: Store) -> dict[str, Any]:
    """Queue a refund for human approval. Nothing here can approve it or move money.

    The amount is not returned, so the model has nothing to promise.
    """
    order = _verified_order(args.order_id, args.email, store)
    blocked = ineligibility(order, args.reason, store)
    if blocked:
        raise ToolError(blocked)
    approval = store.create_refund_approval(order.order_id, args.email, args.reason.strip())
    return {"status": "pending_approval", "ref": approval.id}


def capture_lead(args: CaptureLeadArgs, store: Store) -> dict[str, Any]:
    # The contact details are not echoed back: the model already has them, and
    # keeping them out of results keeps them out of traces too.
    lead = store.add_lead(args.name.strip(), args.email, args.message)
    return {"lead_id": lead.lead_id, "saved": True}
