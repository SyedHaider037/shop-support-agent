"""Consent to save a customer's contact details, decided from the customer's own messages.

Policy 6.2 tells the model to ask first, but a prompt is not a control: capture_lead runs
only when a customer message in this chat clearly agrees. Agreement is either a direct
request ("please save my details", "have someone contact me") or a yes to the assistant's
question about saving or contacting. The latest customer message that speaks to it
decides, so "yes" followed by "actually, don't" is a no. Anything unclear is a no.
"""

import re
from collections.abc import Mapping, Sequence

_DETAILS = r"(?:details|name|email|e-mail|contact|info(?:rmation)?|number|phone)"
_SAVE = r"(?:save|store|keep|record|note|take|pass on|share|send)"

# "please don't save my details", "do not contact me", or a short "no thanks" on its own.
_REFUSES = re.compile(
    rf"\b(?:don't|do not|dont|never|rather (?:you )?not|no need to)\b[^.?!]{{0,40}}"
    rf"\b(?:{_SAVE}|contact|call|e-?mail|reach)\b"
    r"|^\W*(?:no|nope|nah|no thanks?|no,? thank you|not now|not really|i'?d rather not)\b",
    re.IGNORECASE,
)
# "please save my details", "have your team contact me", "get in touch with me".
_ASKS_TO_BE_SAVED = re.compile(
    rf"\b{_SAVE}\b[^.?!]{{0,25}}\b(?:me|my {_DETAILS})\b"
    r"|\b(?:contact|call|e-?mail|reach|ring) me\b"
    r"|\bget (?:back )?in touch with me\b"
    r"|\b(?:have|ask|get) (?:someone|somebody|a person|a human|the team|your team|support)\b"
    r"[^.?!]{0,30}\b(?:contact|call|e-?mail|reach|get in touch)\b",
    re.IGNORECASE,
)
# The assistant asked whether it may save the details or have the team get in touch.
_ASKED_FOR_CONSENT = re.compile(
    rf"\b{_SAVE}\b[^?]{{0,80}}\b{_DETAILS}\b[^?]*\?"
    r"|\b(?:contact|reach|get in touch with|follow up with) you\b[^?]*\?",
    re.IGNORECASE,
)
_YES = re.compile(
    r"^\W*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|of course|absolutely|definitely|please do|"
    r"go ahead|that'?s fine|that would be (?:great|fine|good)|fine|please)\b",
    re.IGNORECASE,
)


def _normalise(text: str) -> str:
    return text.replace("’", "'").strip()


def has_consent(messages: Sequence[Mapping[str, str]]) -> bool:
    """Has the customer clearly agreed, in their own messages, to have their details saved?"""
    consent = False
    asked = False
    for message in messages:
        text = _normalise(str(message.get("content") or ""))
        if message.get("role") == "assistant":
            asked = bool(_ASKED_FOR_CONSENT.search(text))
            continue
        if message.get("role") != "user":
            continue
        if _REFUSES.search(text):
            consent = False
        elif _ASKS_TO_BE_SAVED.search(text) or (asked and _YES.search(text)):
            consent = True
        asked = False
    return consent
