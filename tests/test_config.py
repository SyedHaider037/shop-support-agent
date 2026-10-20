import pytest

from app.config import DEFAULT_MODEL, ConfigError, Settings, create_groq_client, load_settings
from app.trace.redact import mask_email, mask_emails


def test_defaults_when_env_is_empty() -> None:
    settings = load_settings({})

    assert settings == Settings(
        groq_api_key=None, model=DEFAULT_MODEL, max_steps=5, temperature=0.2
    )
    assert settings.request_timeout_s == 20.0


def test_reads_values_from_env() -> None:
    env = {"GROQ_API_KEY": "abc", "MODEL": "other-model", "MAX_STEPS": "3", "TEMPERATURE": "0"}

    settings = load_settings(env)

    assert (settings.model, settings.max_steps, settings.temperature) == ("other-model", 3, 0.0)
    assert settings.groq_api_key == "abc"


@pytest.mark.parametrize("env", [{"MAX_STEPS": "lots"}, {"MAX_STEPS": "0"}, {"TEMPERATURE": "5"}])
def test_invalid_numbers_raise_config_error(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_settings(env)


def test_missing_key_only_fails_when_client_is_created() -> None:
    settings = load_settings({})

    with pytest.raises(ConfigError, match="GROQ_API_KEY is not set"):
        create_groq_client(settings)


def test_client_is_created_with_timeout_when_key_present() -> None:
    client = create_groq_client(Settings(groq_api_key="test-key"))

    assert client.timeout == 20.0


def test_key_is_never_in_repr() -> None:
    assert "super-secret" not in repr(Settings(groq_api_key="super-secret"))


def test_mask_email() -> None:
    assert mask_email("write to jane.doe@example.com today") == "write to j***@example.com today"
    assert mask_emails({"a": ["x@example.com", 3], "b": None}) == {
        "a": ["x***@example.com", 3],
        "b": None,
    }


def test_phase_3_defaults() -> None:
    settings = load_settings({})

    assert settings.reasoning_effort == "low"
    assert settings.request_deadline_s == 40.0
    assert settings.max_retries == 1
    assert settings.token_budget == 20_000
    assert settings.rate_limit_per_minute == 10


def test_reads_phase_3_values_from_env() -> None:
    env = {
        "REASONING_EFFORT": "Medium",
        "REQUEST_DEADLINE_S": "30",
        "MAX_RETRIES": "0",
        "TOKEN_BUDGET": "5000",
        "RATE_LIMIT_PER_MINUTE": "3",
    }

    settings = load_settings(env)

    assert settings.reasoning_effort == "medium"
    assert settings.request_deadline_s == 30.0
    assert settings.max_retries == 0
    assert settings.token_budget == 5000
    assert settings.rate_limit_per_minute == 3


@pytest.mark.parametrize(
    "env",
    [
        {"REASONING_EFFORT": "extreme"},
        {"REQUEST_DEADLINE_S": "0"},
        {"MAX_RETRIES": "-1"},
        {"MAX_RETRIES": "1.5"},
        {"TOKEN_BUDGET": "10"},
        {"RATE_LIMIT_PER_MINUTE": "0"},
    ],
)
def test_invalid_phase_3_values_raise_config_error(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_settings(env)


def test_client_uses_configured_retries() -> None:
    assert create_groq_client(Settings(groq_api_key="test-key", max_retries=0)).max_retries == 0
    assert create_groq_client(Settings(groq_api_key="test-key")).max_retries == 1


@pytest.mark.parametrize(
    ("model", "expected"),
    [("openai/gpt-oss-120b", True), ("openai/gpt-oss-20b", True), ("qwen/qwen3.8-27b", False)],
)
def test_reasoning_effort_applies_only_to_gpt_oss(model: str, expected: bool) -> None:
    assert Settings(model=model).uses_reasoning_effort is expected
