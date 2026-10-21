"""vercel.json must give CDN-served files the same security headers as the app.

On Vercel, files in public/ are served from the CDN without going through the app, so
the security_headers middleware never runs for them. vercel.json sets the headers there
instead; this test fails when the two copies drift apart.
"""

import json
from pathlib import Path

from app.config import Settings
from app.main import SECURITY_HEADERS

ROOT = Path(__file__).resolve().parents[1]


def vercel_config() -> dict:
    return json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))


def test_vercel_json_is_valid_json_with_the_schema() -> None:
    assert vercel_config()["$schema"] == "https://openapi.vercel.sh/vercel.json"


def test_one_header_rule_covers_every_path() -> None:
    rules = vercel_config()["headers"]

    assert [rule["source"] for rule in rules] == ["/(.*)"]


def test_cdn_headers_match_the_app_headers_exactly() -> None:
    (rule,) = vercel_config()["headers"]
    cdn = {header["key"]: header["value"] for header in rule["headers"]}

    assert len(cdn) == len(rule["headers"]), "a header is listed twice"
    assert cdn == SECURITY_HEADERS


def test_the_function_may_run_longer_than_a_chat_request() -> None:
    functions = vercel_config()["functions"]

    assert list(functions) == ["app/main.py"]
    assert functions["app/main.py"]["maxDuration"] > Settings().request_deadline_s
