"""The API docs are off: /docs, /redoc and /openapi.json would publish the admin paths.

The live check of the Vercel demo found all three public (200), with the schema listing
/admin/approvals and its approve and reject routes.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import PUBLIC_DIR, SECURITY_HEADERS, create_app
from app.store.store import Store
from tests.fakes import FakeGroq, reply

DOCS_PATHS = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]


def client(admin_token: str | None = None, fake: FakeGroq | None = None) -> TestClient:
    settings = Settings(groq_api_key="test-key", admin_token=admin_token)
    app = create_app(
        settings=settings, client=fake or FakeGroq(), store=Store.fresh(), log=lambda line: None
    )
    return TestClient(app)


@pytest.mark.parametrize("path", DOCS_PATHS)
@pytest.mark.parametrize("admin_token", [None, "test-admin-token-123"])
def test_docs_paths_are_404(path: str, admin_token: str | None) -> None:
    response = client(admin_token).get(path)

    assert response.status_code == 404
    assert "admin" not in response.text


def test_the_app_publishes_no_schema() -> None:
    app = create_app(settings=Settings(groq_api_key="test-key"), client=FakeGroq())

    assert (app.docs_url, app.redoc_url, app.openapi_url) == (None, None, None)


def test_health_and_chat_still_work() -> None:
    c = client(fake=FakeGroq(reply("Hello!")))

    assert c.get("/health").json() == {"ok": True}
    chat = c.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert chat.status_code == 200
    assert chat.json()["reply"] == "Hello!"
    assert c.post("/chat").status_code == 422


def test_admin_routes_are_still_404_without_a_token() -> None:
    c = client()

    assert c.get("/admin/approvals").status_code == 404
    assert c.post("/admin/approvals/RF-0001/approve").status_code == 404
    assert c.post("/admin/approvals/RF-0001/reject").status_code == 404


def test_the_chat_page_still_loads_with_the_security_headers() -> None:
    assert PUBLIC_DIR.is_dir()
    response = client().get("/")

    assert response.status_code == 200
    assert "<title>" in response.text
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get_list(name) == [value]
