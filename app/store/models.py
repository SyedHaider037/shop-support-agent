"""Typed records for the sample shop data.

Validating the JSON at load time means a typo in the sample data fails fast
in tests instead of surfacing as a confusing tool result mid-conversation.
"""

from datetime import date, datetime
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


class Lead(BaseModel):
    lead_id: str
    name: str
    email: str
    message: str | None
    created_at: datetime
