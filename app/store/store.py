"""In-memory shop store.

Everything lives in memory because the live deploy is serverless (no durable
disk) and evals need a clean, isolated store for every trial.
"""

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import TypeAdapter

from app.store.models import (
    Actor,
    AuditAction,
    AuditEntry,
    Lead,
    Order,
    PendingApproval,
    Product,
    SampleOrder,
)
from app.trace.redact import mask_email

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

_PRODUCTS = TypeAdapter(list[Product])
_SAMPLE_ORDERS = TypeAdapter(list[SampleOrder])
_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> set[str]:
    """Lower-case words with a naive plural strip, so "wallets" finds "wallet"."""
    words = _WORD.findall(text.lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Store:
    """Products, orders, leads and refund approvals for one process or one eval trial.

    `today` anchors the sample dates and refund windows; `now` stamps records.
    Both are injectable so tests and evals are deterministic.
    """

    def __init__(
        self,
        products: list[Product],
        orders: list[Order],
        today: date | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._products = [p.model_copy(deep=True) for p in products]
        self._orders = {o.order_id.upper(): o.model_copy(deep=True) for o in orders}
        self._leads: list[Lead] = []
        self._approvals: dict[str, PendingApproval] = {}
        self._audit: list[AuditEntry] = []
        self.today = today or date.today()
        self.now = now

    @classmethod
    def from_files(
        cls,
        data_dir: Path = DATA_DIR,
        today: date | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> "Store":
        """Load the sample data, resolving order dates against `today` (default: the real today)."""
        today = today or date.today()
        products = _PRODUCTS.validate_json((data_dir / "products.json").read_bytes())
        samples = _SAMPLE_ORDERS.validate_json((data_dir / "orders.json").read_bytes())
        return cls(products, [sample.resolve(today) for sample in samples], today, now)

    @classmethod
    def fresh(cls, today: date | None = None, now: Callable[[], datetime] = _utc_now) -> "Store":
        """A new store from the sample data that shares nothing with any other store."""
        return cls.from_files(today=today, now=now)

    def search_products(self, query: str, max_results: int = 3) -> list[Product]:
        """Rank products by how many query words appear in name, category or description."""
        wanted = _terms(query)
        scored: list[tuple[int, Product]] = []
        for product in self._products:
            haystack = _terms(f"{product.name} {product.category} {product.description}")
            score = len(wanted & haystack)
            if score:
                scored.append((score, product))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [p.model_copy() for _, p in scored[:max_results]]

    def get_order(self, order_id: str) -> Order | None:
        order = self._orders.get(order_id.strip().upper())
        return order.model_copy(deep=True) if order else None

    def add_lead(self, name: str, email: str, message: str | None) -> Lead:
        lead = Lead(
            lead_id=f"LEAD-{len(self._leads) + 1:04d}",
            name=name,
            email=email,
            message=message,
            created_at=self.now(),
        )
        self._leads.append(lead)
        return lead.model_copy()

    def list_leads(self) -> list[Lead]:
        return [lead.model_copy() for lead in self._leads]

    def open_refund_for(self, order_id: str) -> PendingApproval | None:
        """A pending or approved refund for this order, if any (rejected ones do not block)."""
        key = order_id.strip().upper()
        for approval in self._approvals.values():
            if approval.order_id.upper() == key and approval.status != "rejected":
                return approval.model_copy()
        return None

    def create_refund_approval(self, order_id: str, email: str, reason: str) -> PendingApproval:
        """Record a refund request for a human to review. It does not touch the order."""
        order = self._orders[order_id.strip().upper()]
        approval = PendingApproval(
            id=f"RF-{len(self._approvals) + 1:04d}",
            order_id=order.order_id,
            amount=order.total,
            reason=reason,
            status="pending",
            created_at=self.now(),
        )
        self._approvals[approval.id] = approval
        self._log("refund_requested", approval.id, "agent", email)
        return approval.model_copy()

    def list_approvals(self) -> list[PendingApproval]:
        return [a.model_copy() for a in self._approvals.values()]

    def audit_log(self) -> list[AuditEntry]:
        return [entry.model_copy() for entry in self._audit]

    def _log(self, action: AuditAction, approval_id: str, actor: Actor, email: str) -> None:
        self._audit.append(
            AuditEntry(
                at=self.now(),
                action=action,
                approval_id=approval_id,
                actor=actor,
                email=mask_email(email),
            )
        )
