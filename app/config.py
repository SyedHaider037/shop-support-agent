"""Settings from the environment.

A missing GROQ_API_KEY is allowed at import time so tests and /health work
without one; it only becomes an error when a Groq client is actually needed.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from groq import Groq

DEFAULT_MODEL = "llama-3.3-70b-versatile"


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


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    return Settings(
        groq_api_key=env.get("GROQ_API_KEY", "").strip() or None,
        model=env.get("MODEL", "").strip() or DEFAULT_MODEL,
        max_steps=_number(env, "MAX_STEPS", 5, 1, 10),
        temperature=_number(env, "TEMPERATURE", 0.2, 0.0, 2.0),
    )


def create_groq_client(settings: Settings) -> Groq:
    if not settings.groq_api_key:
        raise ConfigError("GROQ_API_KEY is not set. Copy .env.example to .env and add a key.")
    return Groq(api_key=settings.groq_api_key, timeout=settings.request_timeout_s)
