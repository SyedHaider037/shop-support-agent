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
    query: str = Field(min_length=1, max_length=100, description="Search words, e.g. 'wallet'.")
    max_results: int = Field(default=3, ge=1, le=5)


class GetOrderStatusArgs(BaseModel):
    order_id: str = Field(min_length=1, max_length=20, description="e.g. ORD-1001")
    email: str = Field(min_length=3, max_length=254, description="Email used for the order.")


class CaptureLeadArgs(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=254)
    message: str | None = Field(default=None, max_length=1000, description="What they need.")

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
            description="Search the catalog for products, prices and stock.",
            args_model=SearchProductsArgs,
            risk="low",
            read_only=True,
        ),
        ToolSpec(
            name="get_order_status",
            # The schema marks both fields required; policy 4.1 says the same in words.
            description="Get an order's status, items, dates, tracking and total.",
            args_model=GetOrderStatusArgs,
            risk="low",
            read_only=True,
        ),
        ToolSpec(
            name="capture_lead",
            description="Save contact details for human follow-up. Only after consent.",
            args_model=CaptureLeadArgs,
            risk="medium",
            read_only=False,
        ),
    )
}


# Keys left out of the schema the model sees, to save prompt tokens. Titles add nothing;
# length limits are still enforced by the Pydantic models when the call is dispatched.
_MODEL_HIDDEN_KEYS = frozenset({"title", "minLength", "maxLength"})


def _strip_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k not in _MODEL_HIDDEN_KEYS}
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
