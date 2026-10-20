"""Load guardrails.toml and describe what a guardrail did.

The rules live in a plain file so they can be reviewed and tuned without
touching code. A missing or broken file fails loudly: running without
guardrails must never happen silently.
"""

import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

RULES_PATH = Path(__file__).resolve().parents[2] / "guardrails.toml"


class GuardrailConfigError(RuntimeError):
    """guardrails.toml is missing or invalid."""


@dataclass(frozen=True)
class GuardrailEvent:
    layer: Literal["input", "output"]
    rule: str
    action: Literal["refused", "masked", "replaced"]


@dataclass(frozen=True)
class InputPattern:
    id: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class ActionClaimRule:
    id: str
    regex: re.Pattern[str]
    always: bool  # replace even when a write tool succeeded (e.g. "I approved it")


@dataclass(frozen=True)
class GuardrailRules:
    max_messages: int
    max_message_chars: int
    input_refusal: str
    output_leak_refusal: str
    input_patterns: tuple[InputPattern, ...]
    leak_ngram_words: int
    allowed_policy_phrases: tuple[str, ...]
    action_claim_refusal: str
    action_claims: tuple[ActionClaimRule, ...]


def _require(table: dict[str, Any], key: str, kind: type, where: str) -> Any:
    value = table.get(key)
    if not isinstance(value, kind) or isinstance(value, bool) or (kind is str and not value):
        raise GuardrailConfigError(f"{where}.{key} must be a non-empty {kind.__name__}")
    return value


def _patterns(raw: Any) -> tuple[InputPattern, ...]:
    if not isinstance(raw, list) or not raw:
        raise GuardrailConfigError("input_patterns must be a non-empty list of tables")
    patterns: list[InputPattern] = []
    for index, entry in enumerate(raw):
        where = f"input_patterns[{index}]"
        if not isinstance(entry, dict):
            raise GuardrailConfigError(f"{where} must be a table")
        rule_id = _require(entry, "id", str, where)
        try:
            regex = re.compile(_require(entry, "pattern", str, where), re.IGNORECASE)
        except re.error as exc:
            raise GuardrailConfigError(f"{where} ({rule_id}) is not a valid regex: {exc}") from exc
        patterns.append(InputPattern(rule_id, regex))
    ids = [p.id for p in patterns]
    if len(ids) != len(set(ids)):
        raise GuardrailConfigError("input_patterns ids must be unique")
    return tuple(patterns)


def _action_claims(raw: Any) -> tuple[ActionClaimRule, ...]:
    if not isinstance(raw, list) or not raw:
        raise GuardrailConfigError("action_claims must be a non-empty list of tables")
    claims: list[ActionClaimRule] = []
    for index, entry in enumerate(raw):
        where = f"action_claims[{index}]"
        if not isinstance(entry, dict):
            raise GuardrailConfigError(f"{where} must be a table")
        rule_id = _require(entry, "id", str, where)
        always = entry.get("always", False)
        if not isinstance(always, bool):
            raise GuardrailConfigError(f"{where}.always must be true or false")
        try:
            regex = re.compile(_require(entry, "pattern", str, where), re.IGNORECASE)
        except re.error as exc:
            raise GuardrailConfigError(f"{where} ({rule_id}) is not a valid regex: {exc}") from exc
        claims.append(ActionClaimRule(rule_id, regex, always))
    if len({c.id for c in claims}) != len(claims):
        raise GuardrailConfigError("action_claims ids must be unique")
    return tuple(claims)


def _phrases(output: dict[str, Any]) -> tuple[str, ...]:
    raw = output.get("allowed_policy_phrases", [])
    if not isinstance(raw, list) or not all(isinstance(p, str) and p for p in raw):
        raise GuardrailConfigError("output.allowed_policy_phrases must be a list of strings")
    return tuple(raw)


def load_rules(path: Path = RULES_PATH) -> GuardrailRules:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GuardrailConfigError(f"Guardrail rules not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise GuardrailConfigError(f"Guardrail rules are not valid TOML: {exc}") from exc

    limits = data.get("limits")
    refusals = data.get("refusals")
    if not isinstance(limits, dict) or not isinstance(refusals, dict):
        raise GuardrailConfigError("guardrails.toml needs [limits] and [refusals] tables")
    max_messages = _require(limits, "max_messages", int, "limits")
    max_message_chars = _require(limits, "max_message_chars", int, "limits")
    input_refusal = _require(refusals, "input", str, "refusals")
    output_leak_refusal = _require(refusals, "output_leak", str, "refusals")
    patterns = _patterns(data.get("input_patterns"))

    output = data.get("output")
    if not isinstance(output, dict):
        raise GuardrailConfigError("guardrails.toml needs an [output] table")
    ngram = _require(output, "leak_ngram_words", int, "output")
    # Checked after [output] so errors still appear in file order.
    action_claim_refusal = _require(refusals, "action_claim", str, "refusals")
    if ngram < 3:
        raise GuardrailConfigError("output.leak_ngram_words must be at least 3")
    return GuardrailRules(
        max_messages=max_messages,
        max_message_chars=max_message_chars,
        input_refusal=input_refusal,
        output_leak_refusal=output_leak_refusal,
        input_patterns=patterns,
        leak_ngram_words=ngram,
        allowed_policy_phrases=_phrases(output),
        action_claim_refusal=action_claim_refusal,
        action_claims=_action_claims(data.get("action_claims")),
    )


@cache
def guardrail_rules() -> GuardrailRules:
    """The bundled rules, read once per process (they never change at runtime)."""
    return load_rules()
