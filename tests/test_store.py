import json
from datetime import date, timedelta

from app.store.store import DATA_DIR, Store


def test_loads_sample_data() -> None:
    store = Store.fresh()

    assert store.get_order("ORD-1001") is not None
    assert len(store.search_products("wallet", max_results=10)) == 3
    assert store.list_leads() == []


def test_sample_orders_use_example_com_emails_only() -> None:
    orders = json.loads((DATA_DIR / "orders.json").read_text(encoding="utf-8"))

    assert orders
    assert all(order["email"].endswith("@example.com") for order in orders)


def test_sample_orders_cover_every_status() -> None:
    orders = json.loads((DATA_DIR / "orders.json").read_text(encoding="utf-8"))

    assert {o["status"] for o in orders} == {"processing", "shipped", "delivered", "cancelled"}


def test_search_matches_plurals_and_categories() -> None:
    store = Store.fresh()

    names = {p.name for p in store.search_products("wallets", max_results=10)}
    assert "Classic Bifold Wallet" in names
    assert [p.category for p in store.search_products("shoes", max_results=10)] == [
        "shoes",
        "shoes",
    ]


def test_search_respects_max_results_and_ranks_best_match_first() -> None:
    store = Store.fresh()

    results = store.search_products("leather messenger bag", max_results=2)

    assert len(results) == 2
    assert results[0].name == "Leather Messenger Bag"


def test_search_with_no_match_returns_empty() -> None:
    assert Store.fresh().search_products("telescope") == []


def test_get_order_is_case_insensitive_and_unknown_returns_none() -> None:
    store = Store.fresh()

    assert store.get_order(" ord-1002 ") is not None
    assert store.get_order("ORD-9999") is None


def test_add_and_list_leads() -> None:
    store = Store.fresh()

    lead = store.add_lead("Test User", "test@example.com", "Bulk order")

    assert lead.lead_id == "LEAD-0001"
    assert [saved.email for saved in store.list_leads()] == ["test@example.com"]


def test_fresh_stores_are_isolated() -> None:
    first = Store.fresh()
    second = Store.fresh()

    first.add_lead("Test User", "test@example.com", None)
    order = first.get_order("ORD-1001")
    assert order is not None
    order.status = "cancelled"

    assert second.list_leads() == []
    for store in (first, second):
        reloaded = store.get_order("ORD-1001")
        assert reloaded is not None
        assert reloaded.status == "delivered"


def test_returned_lists_cannot_mutate_the_store() -> None:
    store = Store.fresh()
    store.add_lead("Test User", "test@example.com", None)

    store.list_leads().clear()

    assert len(store.list_leads()) == 1


def test_order_dates_resolve_against_the_given_day() -> None:
    store = Store.fresh(today=date(2030, 1, 10))

    order = store.get_order("ORD-1001")  # ordered 15, shipped 13, delivered 8 days ago

    assert order is not None
    assert order.order_date == date(2029, 12, 26)
    assert order.shipped_date == date(2029, 12, 28)
    assert order.delivered_date == date(2030, 1, 2)


def test_missing_dates_stay_missing() -> None:
    order = Store.fresh(today=date(2030, 1, 10)).get_order("ORD-1003")

    assert order is not None
    assert (order.status, order.shipped_date, order.delivered_date) == ("processing", None, None)


def test_dates_default_to_the_real_today() -> None:
    order = Store.fresh().get_order("ORD-1001")

    assert order is not None
    assert order.delivered_date == date.today() - timedelta(days=8)


def test_sample_file_stores_offsets_not_dates() -> None:
    orders = json.loads((DATA_DIR / "orders.json").read_text(encoding="utf-8"))

    for order in orders:
        assert "delivered_date" not in order
        assert isinstance(order["order_days_ago"], int)
        assert order["status"] != "delivered" or order["delivered_days_ago"] is not None


def test_sample_data_covers_recent_and_old_deliveries() -> None:
    orders = json.loads((DATA_DIR / "orders.json").read_text(encoding="utf-8"))
    delivered = [o["delivered_days_ago"] for o in orders if o["status"] == "delivered"]

    assert min(delivered) <= 14
    assert max(delivered) >= 60
