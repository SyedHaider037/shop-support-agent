"""Tool argument models, risk levels and the JSON schemas sent to the model.

Schemas are generated from the Pydantic models so the contract the model sees
and the validation the server applies can never drift apart.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Risk = Literal["low", "medium", "high"]

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class SearchProductsArgs(BaseModel):
    query: str = Field(
        min_length=1, max_length=100, description="Words to search for, e.g. 'leather wallet'."
    )
    max_results: int = Field(default=3, ge=1, le=5, description="How many products to return.")


class GetOrderStatusArgs(BaseModel):
    order_id: str = Field(min_length=1, max_length=20, description="The order ID, e.g. ORD-1001.")
    email: str = Field(
        min_length=3, max_length=254, description="The email address used to place the order."
    )


class CaptureLeadArgs(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="The customer's name.")
    email: str = Field(max_length=254, description="The customer's email address.")
    message: str | None = Field(
        default=None, max_length=1000, description="What the customer needs help with."
    )

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        value = value.strip()
        if not EMAIL_PATTERN.match(value):
            raise ValueError("must be a valid email address")
        return value


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    risk: Risk
    read_only: bool


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="search_products",
            description=(
                "Search the shop's product catalog. Use it for any question about products, "
                "prices or stock. Returns name, category, price, description and in_stock."
            ),
            args_model=SearchProductsArgs,
            risk="low",
            read_only=True,
        ),
        ToolSpec(
            name="get_order_status",
            description=(
                "Look up an order's status, items, dates, tracking number and total. "
                "Requires BOTH the order ID and the email address used for the order."
            ),
            args_model=GetOrderStatusArgs,
            risk="low",
            read_only=True,
        ),
        ToolSpec(
            name="capture_lead",
            description=(
                "Save a customer's name, email and message so a human can follow up. "
                "Only call it after the customer has agreed to share their contact details."
            ),
            args_model=CaptureLeadArgs,
            risk="medium",
            read_only=False,
        ),
    )
}


def _strip_titles(node: Any) -> Any:
    """Drop Pydantic's auto-generated "title" keys; they cost prompt tokens and add nothing."""
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def tool_schemas() -> list[dict[str, Any]]:
    """Tool definitions in the Groq/OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": _strip_titles(spec.args_model.model_json_schema()),
            },
        }
        for spec in TOOLS.values()
    ]
