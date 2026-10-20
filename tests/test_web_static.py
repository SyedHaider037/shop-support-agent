"""Static checks on public/: the page must stay safe under the strict CSP.

These read the files only. They catch the mistakes that would let untrusted model text
become markup or code: inline scripts and styles, HTML sinks in app.js, external
resources, and unlabelled form controls.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

PUBLIC = Path(__file__).resolve().parents[1] / "public"
HTML = (PUBLIC / "index.html").read_text(encoding="utf-8")
JS = (PUBLIC / "app.js").read_text(encoding="utf-8")
CSS = (PUBLIC / "style.css").read_text(encoding="utf-8")
ALLOWED_URLS = {
    "https://github.com/SyedHaider037/shop-support-agent",
    "https://github.com/SyedHaider037/shop-support-agent/blob/main/evals/RESULTS.md",
}


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.script_text: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._in_script = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.script_text.append(data)


def parsed() -> PageParser:
    parser = PageParser()
    parser.feed(HTML)
    return parser


def test_no_inline_script_content() -> None:
    page = parsed()
    scripts = [attrs for tag, attrs in page.tags if tag == "script"]

    assert scripts, "the page should load app.js"
    assert all(attrs.get("src") for attrs in scripts)
    assert "".join(page.script_text).strip() == ""


def test_no_inline_styles_or_event_handlers() -> None:
    page = parsed()

    assert not [tag for tag, _ in page.tags if tag == "style"]
    for tag, attrs in page.tags:
        assert "style" not in attrs, tag
        assert not [name for name in attrs if name.startswith("on")], (tag, attrs)
    assert 'setAttribute("style"' not in JS and "setAttribute('style'" not in JS


def test_no_external_urls_except_the_two_github_links() -> None:
    for name, text in (("index.html", HTML), ("app.js", JS), ("style.css", CSS)):
        urls = set(re.findall(r"https?://[^\s\"'<>)]+", text))
        assert urls <= ALLOWED_URLS, (name, urls - ALLOWED_URLS)
    assert "@import" not in CSS
    assert "url(" not in CSS


def test_app_js_never_parses_text_as_html_or_code() -> None:
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", JS, flags=re.S)  # ignore comments

    for sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "createContextualFragment",
        "DOMParser",
    ):
        assert sink not in code, sink
    assert not re.search(r"\beval\s*\(", code)
    assert not re.search(r"\bnew\s+Function\s*\(", code)
    assert not re.search(r"set(?:Timeout|Interval)\s*\(\s*[\"'`]", code)


def test_app_js_keeps_no_state_in_the_browser() -> None:
    for store in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        assert store not in JS, store


def test_every_form_control_has_a_label() -> None:
    page = parsed()
    labelled = {attrs.get("for") for tag, attrs in page.tags if tag == "label"}

    controls = [(t, a) for t, a in page.tags if t in ("input", "textarea", "select")]
    assert controls
    for tag, attrs in controls:
        assert attrs.get("id") in labelled or attrs.get("aria-label"), (tag, attrs)


def test_buttons_declare_their_type() -> None:
    buttons = [attrs for tag, attrs in parsed().tags if tag == "button"]

    assert buttons
    assert all(attrs.get("type") in ("button", "submit") for attrs in buttons)


def test_page_basics() -> None:
    page = parsed()
    html_tag = next(attrs for tag, attrs in page.tags if tag == "html")
    metas = [attrs for tag, attrs in page.tags if tag == "meta"]

    assert html_tag.get("lang") == "en"
    assert any(
        m.get("name") == "viewport" and "width=device-width" in (m.get("content") or "")
        for m in metas
    )
    assert "<title>" in HTML


def test_replies_are_announced_and_the_fake_data_warning_is_shown() -> None:
    assert re.search(r'id="messages"[^>]*aria-live="polite"', HTML)
    assert "Do not enter real personal information." in HTML
    assert "fake" in HTML.lower()


def test_sample_prompts_use_real_sample_orders() -> None:
    import json

    orders = json.loads((PUBLIC.parent / "data" / "orders.json").read_text(encoding="utf-8"))
    owners = {o["order_id"]: o["email"] for o in orders}
    prompts = re.findall(r'data-prompt="([^"]*)"', HTML)

    assert len(prompts) == 6
    for prompt in prompts:
        for order_id in re.findall(r"ORD-\d{4}", prompt):
            assert order_id in owners, order_id
            assert owners[order_id] in prompt, (order_id, prompt)
