"""A simulated customer: a second model plays the user, as in tau-bench.

Scripted turns test what the agent does with a fixed message; a simulated
customer tests whether the agent asks for what it needs and uses the answer.
It runs on a different model so it does not eat the main model's quota.
"""

from typing import Any

import groq

from app.agent.model_call import retry_after_seconds
from evals.models import CustomerSpec
from evals.runner import InfraError

END = "END_CONVERSATION"
OPENING = "(The support chat has opened. Write your first message to the assistant.)"

PROMPT = """You are role-playing a CUSTOMER chatting with an online shop's support assistant.
Persona: {persona}
Your goal: {goal}
Facts you know:
{facts}

Rules:
- Reply in one or two short sentences, as the customer. Never act as the assistant.
- Give a fact ONLY when the assistant asks for it, exactly as written above. Do not offer
  facts in your first message.
- Never invent facts. If asked for something not listed, say you don't have it.
- When your goal is done, or the assistant has given a final answer or refused, reply with
  exactly {end} and nothing else."""


def system_prompt(spec: CustomerSpec) -> str:
    facts = "\n".join(f"- {key}: {value}" for key, value in spec.facts.items()) or "- (none)"
    return PROMPT.format(persona=spec.persona, goal=spec.goal, facts=facts, end=END)


class SimulatedCustomer:
    def __init__(self, spec: CustomerSpec, client: Any, model: str) -> None:
        self.spec = spec
        self._client = client
        self.model = model

    def _messages(self, history: list[dict[str, str]]) -> list[dict[str, str]]:
        """The agent's conversation seen from the customer's side: roles are swapped."""
        swapped = {"user": "assistant", "assistant": "user"}
        return [
            {"role": "system", "content": system_prompt(self.spec)},
            {"role": "user", "content": OPENING},
            *({"role": swapped[m["role"]], "content": m["content"]} for m in history),
        ]

    def next_message(self, history: list[dict[str, str]]) -> str | None:
        """The customer's next message, or None when they are done."""
        extra: dict[str, Any] = {}
        if self.model.startswith("openai/gpt-oss"):
            extra["reasoning_effort"] = "low"
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=self._messages(history),
                temperature=0.3,
                max_completion_tokens=300,
                timeout=20,
                **extra,
            )
        except groq.RateLimitError as exc:
            raise InfraError("sim_busy", retry_after_seconds(exc)) from exc
        except groq.APIError as exc:
            raise InfraError("sim_error") from exc

        text = (response.choices[0].message.content or "").strip()
        if END in text:
            return None
        if not text:
            # A silent customer is a simulator failure, not something the agent did.
            raise InfraError("sim_empty")
        return text
