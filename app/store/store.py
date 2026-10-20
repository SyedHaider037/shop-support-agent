"""In-memory shop store.

Everything lives in memory because the live deploy is serverless (no durable
disk) and evals need a clean, isolated store for every trial.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from app.store.models import Lead, Order, Product

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

_PRODUCTS = TypeAdapter(list[Product])
_ORDERS = TypeAdapter(list[Order])
_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> set[str]:
    """Lower-case words with a naive plural strip, so "wallets" finds "wallet"."""
    words = _WORD.findall(text.lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}


class Store:
    """Products, orders and leads for one process or one eval trial."""

    def __init__(self, products: list[Product], orders: list[Order]) -> None:
        self._products = [p.model_copy(deep=True) for p in products]
        self._orders = {o.order_id.upper(): o.model_copy(deep=True) for o in orders}
        self._leads: list[Lead] = []

    @classmethod
    def from_files(cls, data_dir: Path = DATA_DIR) -> "Store":
        products = _PRODUCTS.validate_json((data_dir / "products.json").read_bytes())
        orders = _ORDERS.validate_json((data_dir / "orders.json").read_bytes())
        return cls(products, orders)

    @classmethod
    def fresh(cls) -> "Store":
        """A new store from the sample data that shares nothing with any other store."""
        return cls.from_files()

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
            created_at=datetime.now(UTC),
        )
        self._leads.append(lead)
        return lead.model_copy()

    def list_leads(self) -> list[Lead]:
        return [lead.model_copy() for lead in self._leads]
