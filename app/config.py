"""Settings from the environment.

A missing GROQ_API_KEY is allowed at import time so tests and /health work
without one; it only becomes an error when a Groq client is actually needed.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

from groq import Groq

DEFAULT_MODEL = "openai/gpt-oss-120b"

ReasoningEffort = Literal["low", "medium", "high"]
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = ("low", "medium", "high")
# Only these models accept reasoning_effort; others reject or ignore it.
REASONING_MODEL_PREFIX = "openai/gpt-oss"


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    # repr=False keeps the key out of logs, tracebacks and debug prints.
    groq_api_key: str | None = field(default=None, repr=False)
    model: str = DEFAULT_MODEL
    max_steps: int = 5
    temperature: float = 0.2
    request_timeout_s: float = 20.0
    # Low effort keeps tool-calling turns fast; higher effort mostly adds latency here.
    reasoning_effort: ReasoningEffort = "low"
    # Wall-clock cap for a whole /chat request, across every model call and retry.
    request_deadline_s: float = 40.0
    # Retries for Groq 429 and 5xx, done by our loop (the SDK's own retries are off)
    # so every wait is checked against the request deadline.
    max_retries: int = 1
    token_budget: int = 20_000
    rate_limit_per_minute: int = 10
    # Unset means the admin endpoints do not exist (404). repr=False: never logged.
    admin_token: str | None = field(default=None, repr=False)

    @property
    def uses_reasoning_effort(self) -> bool:
        return self.model.startswith(REASONING_MODEL_PREFIX)


def _number[T: (int, float)](env: Mapping[str, str], name: str, default: T, low: T, high: T) -> T:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = type(default)(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _reasoning_effort(env: Mapping[str, str]) -> ReasoningEffort:
    raw = env.get("REASONING_EFFORT", "").strip().lower() or "low"
    if raw not in REASONING_EFFORTS:
        raise ConfigError(f"REASONING_EFFORT must be one of {', '.join(REASONING_EFFORTS)}")
    return cast(ReasoningEffort, raw)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    return Settings(
        groq_api_key=env.get("GROQ_API_KEY", "").strip() or None,
        model=env.get("MODEL", "").strip() or DEFAULT_MODEL,
        max_steps=_number(env, "MAX_STEPS", 5, 1, 10),
        temperature=_number(env, "TEMPERATURE", 0.2, 0.0, 2.0),
        reasoning_effort=_reasoning_effort(env),
        request_deadline_s=_number(env, "REQUEST_DEADLINE_S", 40.0, 1.0, 300.0),
        max_retries=_number(env, "MAX_RETRIES", 1, 0, 5),
        token_budget=_number(env, "TOKEN_BUDGET", 20_000, 1_000, 1_000_000),
        rate_limit_per_minute=_number(env, "RATE_LIMIT_PER_MINUTE", 10, 1, 1_000),
        admin_token=env.get("ADMIN_TOKEN", "").strip() or None,
    )


def create_groq_client(settings: Settings) -> Groq:
    if not settings.groq_api_key:
        raise ConfigError("GROQ_API_KEY is not set. Copy .env.example to .env and add a key.")
    return Groq(
        api_key=settings.groq_api_key,
        timeout=settings.request_timeout_s,
        # Off: the SDK would wait out retry-after without knowing our deadline.
        max_retries=0,
    )
