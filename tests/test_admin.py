import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.main import create_app
from app.store.store import Store
from app.tools.dispatcher import dispatch
from tests.fakes import FakeGroq

TOKEN = "test-admin-token-123"
AUTH = {"X-Admin-Token": TOKEN}
REQUEST = {"order_id": "ORD-1001", "email": "jane.doe@example.com", "reason": "Broken zip"}


@pytest.fixture
def store() -> Store:
    store = Store.fresh(today=date(2026, 10, 20))
    assert dispatch("request_refund", json.dumps(REQUEST), store)["ok"] is True
    return store


def admin_client(store: Store, token: str | None = TOKEN) -> TestClient:
    settings = Settings(groq_api_key="test-key", admin_token=token)
    return TestClient(create_app(settings=settings, client=FakeGroq(), store=store))


def test_admin_endpoints_are_disabled_without_a_token(store: Store) -> None:
    client = admin_client(store, token=None)

    assert client.get("/admin/approvals", headers=AUTH).status_code == 404
    assert client.post("/admin/approvals/RF-0001/approve", headers=AUTH).status_code == 404
    assert client.post("/admin/approvals/RF-0001/reject", headers=AUTH).status_code == 404
    assert store.list_approvals()[0].status == "pending"


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}, {"X-Admin-Token": ""}])
def test_wrong_or_missing_token_is_401(store: Store, headers: dict[str, str]) -> None:
    client = admin_client(store)

    assert client.get("/admin/approvals", headers=headers).status_code == 401
    assert client.post("/admin/approvals/RF-0001/approve", headers=headers).status_code == 401
    assert store.list_approvals()[0].status == "pending"


def test_list_approvals_with_masked_email(store: Store) -> None:
    response = admin_client(store).get("/admin/approvals", headers=AUTH)

    assert response.status_code == 200
    [item] = response.json()
    assert item["id"] == "RF-0001"
    assert item["order_id"] == "ORD-1001"
    assert item["amount"] == 39.99
    assert item["status"] == "pending"
    assert item["email"] == "j***@example.com"
    assert "jane.doe@example.com" not in response.text


def test_emails_inside_the_reason_are_masked_too() -> None:
    store = Store.fresh(today=date(2026, 10, 20))
    dispatch(
        "request_refund",
        json.dumps({**REQUEST, "reason": "Email me at jane.doe@example.com"}),
        store,
    )

    response = admin_client(store).get("/admin/approvals", headers=AUTH)

    assert "jane.doe@example.com" not in response.text


def test_approve_changes_the_order_and_writes_the_audit_log(store: Store) -> None:
    response = admin_client(store).post("/admin/approvals/RF-0001/approve", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["decided_at"] is not None
    order = store.get_order("ORD-1001")
    assert order is not None and order.refund_status == "refunded"
    entries = [(e.action, e.actor, e.email) for e in store.audit_log()]
    assert entries == [
        ("refund_requested", "agent", "j***@example.com"),
        ("refund_approved", "admin", "j***@example.com"),
    ]


def test_reject_does_not_change_the_order(store: Store) -> None:
    response = admin_client(store).post("/admin/approvals/RF-0001/reject", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    order = store.get_order("ORD-1001")
    assert order is not None and order.refund_status is None
    assert [e.action for e in store.audit_log()] == ["refund_requested", "refund_rejected"]


@pytest.mark.parametrize(
    ("first", "second"),
    [("approve", "approve"), ("approve", "reject"), ("reject", "approve"), ("reject", "reject")],
)
def test_a_decision_is_final(store: Store, first: str, second: str) -> None:
    client = admin_client(store)
    assert client.post(f"/admin/approvals/RF-0001/{first}", headers=AUTH).status_code == 200

    repeat = client.post(f"/admin/approvals/RF-0001/{second}", headers=AUTH)

    assert repeat.status_code == 409
    assert len(store.audit_log()) == 2


def test_unknown_approval_is_404(store: Store) -> None:
    client = admin_client(store)

    assert client.post("/admin/approvals/RF-9999/approve", headers=AUTH).status_code == 404
    assert client.post("/admin/approvals/RF-9999/reject", headers=AUTH).status_code == 404


def test_refunded_order_cannot_be_requested_again(store: Store) -> None:
    admin_client(store).post("/admin/approvals/RF-0001/approve", headers=AUTH)

    assert dispatch("request_refund", json.dumps(REQUEST), store)["error"] == "already_requested"


def test_rejected_request_can_be_made_again(store: Store) -> None:
    admin_client(store).post("/admin/approvals/RF-0001/reject", headers=AUTH)

    assert dispatch("request_refund", json.dumps(REQUEST), store)["ok"] is True


def test_admin_endpoints_are_not_rate_limited(store: Store) -> None:
    settings = Settings(groq_api_key="k", admin_token=TOKEN, rate_limit_per_minute=1)
    client = TestClient(create_app(settings=settings, client=FakeGroq(), store=store))

    assert all(client.get("/admin/approvals", headers=AUTH).status_code == 200 for _ in range(5))


def test_admin_token_is_read_from_env_and_never_in_repr() -> None:
    settings = load_settings({"ADMIN_TOKEN": " secret-admin-value "})

    assert settings.admin_token == "secret-admin-value"
    assert "secret-admin-value" not in repr(settings)
    assert load_settings({}).admin_token is None
