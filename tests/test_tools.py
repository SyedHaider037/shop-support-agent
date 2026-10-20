import json
from datetime import date
from typing import Any

import pytest

from app.store.store import Store
from app.tools import dispatcher
from app.tools.definitions import TOOLS, tool_schemas
from app.tools.dispatcher import dispatch

# The day the sample data was written for, so the expected dates below never drift.
TODAY = date(2026, 10, 20)


@pytest.fixture
def store() -> Store:
    return Store.fresh(today=TODAY)


def call(store: Store, tool: str, /, **arguments: Any) -> dict[str, Any]:
    return dispatch(tool, json.dumps(arguments), store)


# --- schemas and metadata ---------------------------------------------------


def test_schemas_cover_every_tool_in_function_format() -> None:
    schemas = tool_schemas()

    assert [s["function"]["name"] for s in schemas] == list(TOOLS)
    for schema in schemas:
        assert schema["type"] == "function"
        assert schema["function"]["parameters"]["type"] == "object"
        assert "title" not in json.dumps(schema)


def test_required_fields_are_marked_in_schemas() -> None:
    params = {s["function"]["name"]: s["function"]["parameters"] for s in tool_schemas()}

    assert params["search_products"]["required"] == ["query"]
    assert params["search_products"]["properties"]["max_results"]["maximum"] == 5
    assert params["get_order_status"]["required"] == ["order_id", "email"]
    assert params["capture_lead"]["required"] == ["name", "email"]


def test_every_tool_has_a_risk_level_and_a_handler() -> None:
    assert {name: spec.risk for name, spec in TOOLS.items()} == {
        "search_products": "low",
        "get_order_status": "low",
        "capture_lead": "medium",
        "request_refund": "high",
    }
    assert set(dispatcher.HANDLERS) == set(TOOLS)


# --- search_products ----------------------------------------------------------


def test_search_products_happy_path(store: Store) -> None:
    result = call(store, "search_products", query="wallet")

    assert result["ok"] is True
    products = result["data"]["products"]
    assert 1 <= len(products) <= 3
    assert set(products[0]) == {"id", "name", "category", "price", "description", "in_stock"}


def test_search_products_honours_max_results(store: Store) -> None:
    result = call(store, "search_products", query="leather", max_results=5)

    assert len(result["data"]["products"]) == 5


@pytest.mark.parametrize(
    "arguments",
    [{}, {"query": ""}, {"query": "wallet", "max_results": 6}, {"query": "x", "max_results": 0}],
)
def test_search_products_rejects_bad_arguments(store: Store, arguments: dict[str, Any]) -> None:
    result = dispatch("search_products", json.dumps(arguments), store)

    assert result["ok"] is False
    assert result["error"] == "invalid_arguments"


def test_search_products_no_match_is_ok_and_empty(store: Store) -> None:
    assert call(store, "search_products", query="telescope") == {
        "ok": True,
        "data": {"products": []},
    }


# --- get_order_status ---------------------------------------------------------


def test_get_order_status_happy_path(store: Store) -> None:
    result = call(store, "get_order_status", order_id="ORD-1001", email="Jane.Doe@Example.com")

    assert result["ok"] is True
    assert result["data"] == {
        "order_id": "ORD-1001",
        "status": "delivered",
        "items": ["Classic Bifold Wallet"],
        "order_date": "2026-10-05",
        "shipped_date": "2026-10-07",
        "delivered_date": "2026-10-12",
        "tracking_number": "TRK-000-1001",
        "total": 39.99,
    }


def test_get_order_status_never_returns_customer_details(store: Store) -> None:
    result = call(store, "get_order_status", order_id="ORD-1001", email="jane.doe@example.com")

    text = json.dumps(result).lower()
    assert "jane" not in text
    assert "@" not in text


def test_get_order_status_wrong_email(store: Store) -> None:
    result = call(store, "get_order_status", order_id="ORD-1001", email="sam.lee@example.com")

    assert result == {"ok": False, "error": "no_matching_order"}


def test_get_order_status_unknown_order(store: Store) -> None:
    result = call(store, "get_order_status", order_id="ORD-9999", email="jane.doe@example.com")

    assert result == {"ok": False, "error": "no_matching_order"}


def test_order_errors_do_not_reveal_whether_an_order_exists(store: Store) -> None:
    exists_wrong_email = call(
        store, "get_order_status", order_id="ORD-1002", email="nobody@example.com"
    )
    does_not_exist = call(
        store, "get_order_status", order_id="ORD-0000", email="nobody@example.com"
    )

    assert exists_wrong_email == does_not_exist


def test_get_order_status_requires_email(store: Store) -> None:
    result = call(store, "get_order_status", order_id="ORD-1001")

    assert result["error"] == "invalid_arguments"
    assert any(d.startswith("email:") for d in result["detail"])


# --- capture_lead -------------------------------------------------------------


def test_capture_lead_happy_path(store: Store) -> None:
    result = call(
        store, "capture_lead", name="Test User", email="test@example.com", message="Bulk order"
    )

    assert result == {"ok": True, "data": {"lead_id": "LEAD-0001", "saved": True}}
    [lead] = store.list_leads()
    assert (lead.name, lead.email, lead.message) == ("Test User", "test@example.com", "Bulk order")


def test_capture_lead_message_is_optional(store: Store) -> None:
    assert call(store, "capture_lead", name="Test User", email="test@example.com")["ok"] is True


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "two@@example.com", "spa ce@example.com"])
def test_capture_lead_rejects_invalid_email(store: Store, email: str) -> None:
    result = call(store, "capture_lead", name="Test User", email=email)

    assert result["error"] == "invalid_arguments"
    assert store.list_leads() == []


def test_validation_detail_does_not_echo_input(store: Store) -> None:
    result = call(store, "capture_lead", name="Test User", email="secret-value-xyz")

    assert "secret-value-xyz" not in json.dumps(result)


# --- dispatcher failure modes ------------------------------------------------


def test_unknown_tool(store: Store) -> None:
    assert dispatch("delete_everything", "{}", store) == {"ok": False, "error": "unknown_tool"}


@pytest.mark.parametrize("raw", ["{not json", '{"query": "wallet"', "nul"])
def test_malformed_json(store: Store, raw: str) -> None:
    assert dispatch("search_products", raw, store) == {"ok": False, "error": "invalid_json"}


@pytest.mark.parametrize("raw", ["[]", '"wallet"', "42", "null"])
def test_non_object_arguments(store: Store, raw: str) -> None:
    assert dispatch("search_products", raw, store)["error"] == "invalid_arguments"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_empty_arguments_are_treated_as_no_arguments(store: Store, raw: str | None) -> None:
    result = dispatch("search_products", raw, store)

    assert result["error"] == "invalid_arguments"


def test_unexpected_tool_crash_becomes_error_result(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(args: object, store: Store) -> dict[str, Any]:
        raise RuntimeError("bug")

    monkeypatch.setitem(dispatcher.HANDLERS, "search_products", boom)

    assert call(store, "search_products", query="wallet") == {"ok": False, "error": "tool_failed"}
