from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import PUBLIC_DIR, SECURITY_HEADERS, create_app
from app.ratelimit import RateLimiter
from app.store.store import Store
from tests.fakes import FakeClock, FakeGroq, reply

SETTINGS = Settings(groq_api_key="test-key")
PAGE = "<!doctype html><html lang='en'><title>t</title><p>chat page</p></html>"


def client_with(public_dir: Path | None, fake: FakeGroq | None = None, **kwargs: Any) -> TestClient:
    app = create_app(
        settings=SETTINGS,
        client=fake or FakeGroq(),
        store=Store.fresh(),
        log=lambda line: None,
        public_dir=public_dir,
        **kwargs,
    )
    return TestClient(app)


def site(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")
    (tmp_path / "app.js").write_text("console.log('x');", encoding="utf-8")
    return tmp_path


def test_root_serves_the_page(tmp_path: Path) -> None:
    response = client_with(site(tmp_path)).get("/")

    assert response.status_code == 200
    assert "chat page" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_static_files_are_served(tmp_path: Path) -> None:
    response = client_with(site(tmp_path)).get("/app.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_api_routes_keep_priority_over_the_static_mount(tmp_path: Path) -> None:
    client = client_with(site(tmp_path), FakeGroq(reply("Hello!")))

    assert client.get("/health").json() == {"ok": True}
    chat = client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert chat.status_code == 200
    assert chat.json()["reply"] == "Hello!"
    assert client.get("/admin/approvals").status_code == 404  # admin disabled without a token


def test_security_headers_on_every_response(tmp_path: Path) -> None:
    client = client_with(site(tmp_path), FakeGroq(reply("Hello!")))
    responses = [
        client.get("/"),
        client.get("/app.js"),
        client.get("/health"),
        client.get("/missing.txt"),
        client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]}),
        client.post("/chat", json={"messages": []}),  # a 422
    ]

    for response in responses:
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (response.request.url, name)


def test_csp_forbids_inline_and_external_code() -> None:
    csp = SECURITY_HEADERS["Content-Security-Policy"]

    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    assert "frame-ancestors 'none'" in csp


def test_app_starts_without_a_public_folder(tmp_path: Path) -> None:
    client = client_with(tmp_path / "does-not-exist")

    assert client.get("/health").json() == {"ok": True}
    assert client.get("/").status_code == 404


def test_static_files_are_not_rate_limited(tmp_path: Path) -> None:
    limiter = RateLimiter(per_minute=1, clock=FakeClock())
    client = client_with(site(tmp_path), FakeGroq(reply("a"), reply("b")), limiter=limiter)

    assert all(client.get("/").status_code == 200 for _ in range(10))
    assert (
        client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]}).status_code
        == 200
    )
    assert (
        client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]}).status_code
        == 429
    )
    assert client.get("/").status_code == 200


def test_default_public_folder_is_the_repo_one() -> None:
    assert PUBLIC_DIR.name == "public"
    assert PUBLIC_DIR.parent == Path(__file__).resolve().parents[1]


def test_the_real_chat_page_is_served() -> None:
    client = client_with(PUBLIC_DIR)

    page = client.get("/")
    css = client.get("/style.css")

    assert page.status_code == 200
    assert "shop-support-agent" in page.text
    assert "Do not enter real personal information." in page.text
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
