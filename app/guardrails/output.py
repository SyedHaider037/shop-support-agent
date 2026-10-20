"""Layer 3: check the model's final reply before the customer sees it.

Prompt instructions can be talked around, so two things are checked in code:
the reply must not quote the policy, and it must not contain contact details
the customer did not give us themselves (another customer's email, a phone).
"""

import re
from dataclasses import dataclass, field
from functools import cache

from app.agent.policy import load_policy
from app.guardrails.rules import GuardrailEvent, GuardrailRules

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 7 to 15 digits with common separators. Not glued to letters or hyphens, so
# order IDs (ORD-1002) and tracking numbers (TRK-000-1002) are left alone.
_PHONE = re.compile(r"(?<![\w-])\+?\(?\d[\d\s().-]{5,}\d(?![\w-])")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

EMAIL_PLACEHOLDER = "[email removed]"
PHONE_PLACEHOLDER = "[phone removed]"


@dataclass(frozen=True)
class OutputCheck:
    reply: str
    events: list[GuardrailEvent] = field(default_factory=list)


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("’", "'"))


def _ngrams(words: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def policy_ngrams(policy: str, n: int, allowed_phrases: tuple[str, ...]) -> set[tuple[str, ...]]:
    """Every n-word run in the policy, minus runs the agent is told to say to customers."""
    allowed: set[tuple[str, ...]] = set()
    for phrase in allowed_phrases:
        allowed |= _ngrams(_words(phrase), n)
    return _ngrams(_words(policy), n) - allowed


@cache
def _bundled_policy_ngrams(n: int, allowed_phrases: tuple[str, ...]) -> frozenset[tuple[str, ...]]:
    return frozenset(policy_ngrams(load_policy(), n, allowed_phrases))


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _phones(text: str) -> list[str]:
    found = []
    for match in _PHONE.finditer(text):
        candidate = match.group().strip()
        if 7 <= len(_digits(candidate)) <= 15 and not _ISO_DATE.fullmatch(candidate):
            found.append(candidate)
    return found


def check_output(
    reply: str, user_texts: list[str], rules: GuardrailRules, policy: str | None = None
) -> OutputCheck:
    """Return the reply to send, with any guardrail events.

    `policy` defaults to the bundled policy.md; tests pass their own text.
    """
    n = rules.leak_ngram_words
    if policy is None:
        leak_set: set[tuple[str, ...]] | frozenset[tuple[str, ...]] = _bundled_policy_ngrams(
            n, rules.allowed_policy_phrases
        )
    else:
        leak_set = policy_ngrams(policy, n, rules.allowed_policy_phrases)
    if _ngrams(_words(reply), n) & leak_set:
        return OutputCheck(
            reply=rules.output_leak_refusal,
            events=[GuardrailEvent(layer="output", rule="prompt_leak", action="replaced")],
        )

    user_text = "\n".join(user_texts)
    own_emails = {e.lower() for e in _EMAIL.findall(user_text)}
    own_phones = {_digits(p) for p in _phones(user_text)}
    events: list[GuardrailEvent] = []

    def mask_email(match: re.Match[str]) -> str:
        if match.group().lower() in own_emails:
            return match.group()
        events.append(GuardrailEvent(layer="output", rule="stray_email", action="masked"))
        return EMAIL_PLACEHOLDER

    masked = _EMAIL.sub(mask_email, reply)
    for phone in _phones(masked):
        if _digits(phone) not in own_phones:
            masked = masked.replace(phone, PHONE_PLACEHOLDER)
            events.append(GuardrailEvent(layer="output", rule="stray_phone", action="masked"))
    return OutputCheck(reply=masked, events=events)
