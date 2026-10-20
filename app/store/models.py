"""Typed records for the sample shop data.

Validating the JSON at load time means a typo in the sample data fails fast
in tests instead of surfacing as a confusing tool result mid-conversation.
"""

from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

OrderStatus = Literal["processing", "shipped", "delivered", "cancelled"]


class Product(BaseModel):
    id: str
    name: str
    category: str
    price: float
    description: str
    in_stock: bool


class OrderItem(BaseModel):
    product_id: str
    name: str
    quantity: int
    unit_price: float


class Order(BaseModel):
    order_id: str
    customer_name: str
    email: str
    status: OrderStatus
    items: list[OrderItem]
    order_date: date
    shipped_date: date | None
    delivered_date: date | None
    tracking_number: str | None
    total: float


class SampleOrder(BaseModel):
    """An order as stored in data/orders.json: dates are day offsets from "today".

    Fixed dates would go stale, and refund-window rules depend on how long ago
    an order was delivered, so the sample data is anchored to the current day.
    """

    order_id: str
    customer_name: str
    email: str
    status: OrderStatus
    items: list[OrderItem]
    order_days_ago: int
    shipped_days_ago: int | None
    delivered_days_ago: int | None
    tracking_number: str | None
    total: float

    def resolve(self, today: date) -> Order:
        def ago(days: int | None) -> date | None:
            return None if days is None else today - timedelta(days=days)

        fields = self.model_dump(
            exclude={"order_days_ago", "shipped_days_ago", "delivered_days_ago"}
        )
        return Order(
            **fields,
            order_date=today - timedelta(days=self.order_days_ago),
            shipped_date=ago(self.shipped_days_ago),
            delivered_date=ago(self.delivered_days_ago),
        )


class Lead(BaseModel):
    lead_id: str
    name: str
    email: str
    message: str | None
    created_at: datetime
