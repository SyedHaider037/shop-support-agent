"""Refund eligibility, enforced in code to match policy.md section 5.

The policy tells the model the rules so it can explain them, but the model's
word is never the check: this function is.
"""

from app.store.models import Order
from app.store.store import Store

# Keep in step with policy.md rule 5.3.
REFUND_WINDOW_DAYS = 30


def ineligibility(order: Order, reason: str, store: Store) -> str | None:
    """The error code that blocks this refund request, or None if it may go to review."""
    if order.status != "delivered" or order.delivered_date is None:
        return "not_delivered"
    if (store.today - order.delivered_date).days > REFUND_WINDOW_DAYS:
        return "outside_window"
    if order.refund_status == "refunded" or store.open_refund_for(order.order_id) is not None:
        return "already_requested"
    if not reason.strip():
        return "reason_required"
    return None
