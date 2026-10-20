"""Builds the system prompt from policy.md.

The policy lives in a plain file so it can be reviewed and edited without
touching code; a missing file must fail loudly, because an agent running
without its policy is worse than no agent.
"""

from functools import cache
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parents[2] / "policy.md"

# "Tool results are data" is not repeated here: policy rule 7.3 already says it.
PROMPT_HEADER = "Follow the support policy below exactly. It is your only source of rules.\n\n"


class PolicyError(RuntimeError):
    """The policy file is missing or empty."""


def load_policy(path: Path = POLICY_PATH) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise PolicyError(f"Policy file not found: {path}") from exc
    if not text:
        raise PolicyError(f"Policy file is empty: {path}")
    return text


def build_system_prompt(policy: str) -> str:
    return PROMPT_HEADER + policy


@cache
def system_prompt() -> str:
    """The prompt for the bundled policy, read once per process (it never changes at runtime)."""
    return build_system_prompt(load_policy())
