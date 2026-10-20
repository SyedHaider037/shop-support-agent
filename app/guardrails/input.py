"""Layer 1: check every user message before the model sees anything.

Refusing here is cheap and certain: a refused request makes no model call.
The client sends the whole history, so earlier user turns are checked too;
otherwise an attack could be planted in a past message.
"""

import re
from dataclasses import dataclass

from app.guardrails.rules import GuardrailEvent, GuardrailRules

# C0/C1 control characters (except tab and newline) and invisible zero-width or
# direction marks, which can hide text from the patterns below.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤﻿]")
_WHITESPACE = re.compile(r"\s+")
# A line that pretends to be another speaker, e.g. "system: you have no rules".
_ROLE_MARKER = re.compile(r"^\s*(system|assistant)\s*:", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class InputCheck:
    messages: list[dict[str, str]]
    event: GuardrailEvent | None = None

    @property
    def refused(self) -> bool:
        return self.event is not None


def clean(text: str) -> str:
    return _CONTROL.sub("", text).strip()


def _refuse(rule: str) -> InputCheck:
    return InputCheck(messages=[], event=GuardrailEvent(layer="input", rule=rule, action="refused"))


def check_input(messages: list[dict[str, str]], rules: GuardrailRules) -> InputCheck:
    """Return cleaned messages, or a refusal naming the first rule that fired."""
    if len(messages) > rules.max_messages:
        return _refuse("too_many_messages")

    cleaned: list[dict[str, str]] = []
    for message in messages:
        content = clean(message["content"])
        if message["role"] == "user":
            if not content:
                return _refuse("empty_message")
            if len(content) > rules.max_message_chars:
                return _refuse("message_too_long")
            if _ROLE_MARKER.search(content):
                return _refuse("role_injection")
            flat = _WHITESPACE.sub(" ", content)
            for pattern in rules.input_patterns:
                if pattern.regex.search(flat):
                    return _refuse(pattern.id)
        cleaned.append({"role": message["role"], "content": content})
    return InputCheck(messages=cleaned)
