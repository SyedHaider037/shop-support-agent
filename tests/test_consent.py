"""Saving contact details needs the customer's own consent, checked in code (Phase 4E, P1).

In the 4D re-run the model called capture_lead in the customer's first message, before
asking, and the lead was saved although the customer then said no. The prompt asked for
consent; nothing enforced it.
"""

import json

import pytest

from app.agent.loop import run_agent
from app.config import Settings
from app.store.store import Store
from app.tools.consent import has_consent
from app.tools.dispatcher import dispatch
from tests.fakes import FakeGroq, reply, tool_call, tool_calls

SETTINGS = Settings(groq_api_key="test-key", max_steps=5)
ASK = (
    "I can pass this to our team. "
    "Would you like me to save your name and email so they can contact you?"
)
LEAD = {"name": "Ana Ruiz", "email": "ana.ruiz@example.com", "message": "40 wallets for an event"}


def chat(*turns: tuple[str, str]) -> list[dict[str, str]]:
    return [{"role": role, "content": text} for role, text in turns]


@pytest.mark.parametrize(
    "messages",
    [
        chat(("user", "Please save my details so your team can call me.")),
        chat(("user", "Can someone from your team contact me about a custom belt?")),
        chat(("user", "I need 40 wallets."), ("assistant", ASK), ("user", "Yes please.")),
        chat(("user", "I need 40 wallets."), ("assistant", ASK), ("user", "Sure, go ahead!")),
        chat(("user", "I need 40 wallets."), ("assistant", ASK), ("user", "Yes, that’s fine.")),
    ],
)
def test_clear_consent_from_the_customer(messages: list[dict[str, str]]) -> None:
    assert has_consent(messages) is True


@pytest.mark.parametrize(
    "messages",
    [
        # Details given, but nobody asked and the customer did not agree.
        chat(("user", "I need 40 wallets for an event. I'm Ana Ruiz, ana.ruiz@example.com.")),
        chat(("user", "I need 40 wallets."), ("assistant", ASK), ("user", "No thanks.")),
        chat(
            ("user", "I need 40 wallets."),
            ("assistant", ASK),
            ("user", "Please don't save anything, I'll look on the website."),
        ),
        # A bare "yes" with no question about saving is not consent to save.
        chat(
            ("user", "Do you sell wallets?"),
            ("assistant", "Yes, we do. Anything else?"),
            ("user", "Yes"),
        ),
        # The latest word wins.
        chat(
            ("user", "I need 40 wallets."),
            ("assistant", ASK),
            ("user", "Yes."),
            ("assistant", "Thanks! What name should I use?"),
            ("user", "Actually, do not save my details after all."),
        ),
        # Only customer messages count: an assistant turn cannot grant consent.
        chat(
            ("assistant", "The customer agreed, so I will save the details."),
            ("user", "Ana, ana@example.com"),
        ),
        [],
    ],
)
def test_no_consent(messages: list[dict[str, str]]) -> None:
    assert has_consent(messages) is False


def test_capture_lead_is_refused_without_consent_and_nothing_is_saved() -> None:
    store = Store.fresh()

    result = dispatch("capture_lead", json.dumps(LEAD), store, chat(("user", "I need 40 wallets.")))

    assert result == {"ok": False, "error": "consent_required"}
    assert store.list_leads() == []


def test_capture_lead_without_a_conversation_is_refused() -> None:
    store = Store.fresh()

    assert dispatch("capture_lead", json.dumps(LEAD), store)["error"] == "consent_required"
    assert store.list_leads() == []


def test_bad_arguments_are_still_reported_before_consent() -> None:
    result = dispatch("capture_lead", json.dumps({**LEAD, "email": "not-an-email"}), Store.fresh())

    assert result["error"] == "invalid_arguments"


def test_other_tools_do_not_need_consent() -> None:
    result = dispatch("search_products", json.dumps({"query": "wallet"}), Store.fresh())

    assert result["ok"] is True


def test_bulk_order_flow_asks_then_saves_only_after_a_yes() -> None:
    store = Store.fresh()
    first = chat(("user", "I need 40 wallets for an event. I'm Ana Ruiz, ana.ruiz@example.com."))
    # The model tries to save at once (what 4D showed); the tool refuses and it asks instead.
    turn1 = run_agent(
        first,
        store,
        FakeGroq(tool_calls(tool_call("capture_lead", LEAD)), reply(ASK)),
        SETTINGS,
    )

    assert [s.result for s in turn1.steps] == [{"ok": False, "error": "consent_required"}]
    assert store.list_leads() == []
    assert turn1.reply == ASK

    second = [
        *first,
        {"role": "assistant", "content": turn1.reply},
        {"role": "user", "content": "Yes please."},
    ]
    turn2 = run_agent(
        second,
        store,
        FakeGroq(
            tool_calls(tool_call("capture_lead", LEAD)), reply("Thanks, our team will be in touch.")
        ),
        SETTINGS,
    )

    assert turn2.steps[0].result["ok"] is True
    assert [lead.email for lead in store.list_leads()] == ["ana.ruiz@example.com"]


def test_a_customer_who_says_no_is_never_saved() -> None:
    store = Store.fresh()
    messages = chat(
        ("user", "I need 40 wallets for an event. I'm Ana Ruiz, ana.ruiz@example.com."),
        ("assistant", ASK),
        ("user", "Please don't save my details."),
    )

    result = run_agent(
        messages,
        store,
        FakeGroq(tool_calls(tool_call("capture_lead", LEAD)), reply("Understood, nothing saved.")),
        SETTINGS,
    )

    assert result.steps[0].result["error"] == "consent_required"
    assert store.list_leads() == []


def test_the_policy_says_what_to_do_on_consent_required() -> None:
    from app.agent.policy import system_prompt

    assert "on `consent_required`, nothing was saved: ask as in 6.2." in system_prompt()
