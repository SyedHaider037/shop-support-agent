"""A simulated customer: a second model plays the user, as in tau-bench.

Scripted turns test what the agent does with a fixed message; a simulated
customer tests whether the agent asks for what it needs and uses the answer.
It runs on a different model so it does not eat the main model's quota.
"""

import re
from typing import Any

import groq

from app.agent.model_call import is_tool_use_failed, retry_after_seconds
from evals.models import CustomerSpec
from evals.runner import InfraError

END = "END_CONVERSATION"
# gpt-oss-20b as the customer sometimes emits a tool call even with no tools offered; Groq
# rejects it (400 "Tool choice is none, but model called a tool"). Retrying usually works.
TOOL_CALL_RETRIES = 2
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
- When the assistant asks you a question or asks for a detail, answer it: give the fact if
  it is listed above, or say you don't have it. A question is never the end of the chat.
- Never invent facts. If asked for something not listed, say you don't have it.
- Reply with exactly {end} and nothing else only when your goal is done, or the assistant
  has clearly said it cannot help with it. Never end while the assistant is waiting for an
  answer from you.
- Reply with plain text only. Never call a tool."""


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ORDER_ID = re.compile(r"\bORD[-‑]?\d+\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<![\w-])\+?\(?\d[\d\s().-]{5,}\d(?![\w-])")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def invented_facts(text: str, facts: dict[str, str]) -> list[str]:
    """Emails, order IDs and phone numbers the customer wrote that are not in its facts.

    If the simulator makes one up, the agent is being tested on data that does not
    exist, so the run says nothing about the agent.
    """
    known = " ".join(facts.values()).lower()
    known_digits = re.sub(r"\D", "", known)
    found = [m.group() for m in _EMAIL.finditer(text) if m.group().lower() not in known]
    found += [
        m.group()
        for m in _ORDER_ID.finditer(text)
        if m.group().upper().replace("‑", "-") not in known.upper()
    ]
    for match in _PHONE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if _ISO_DATE.fullmatch(match.group().strip()):
            continue
        if 7 <= len(digits) <= 15 and digits not in known_digits:
            found.append(match.group().strip())
    return found


def system_prompt(spec: CustomerSpec) -> str:
    facts = "\n".join(f"- {key}: {value}" for key, value in spec.facts.items()) or "- (none)"
    return PROMPT.format(persona=spec.persona, goal=spec.goal, facts=facts, end=END)


class SimulatedCustomer:
    def __init__(self, spec: CustomerSpec, client: Any, model: str) -> None:
        self.spec = spec
        self._client = client
        self.model = model
        self.retries = 0  # simulator retries after a stray tool call, for the result row

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
        for attempt in range(TOOL_CALL_RETRIES + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=self._messages(history),
                    temperature=0.3,
                    max_completion_tokens=300,
                    timeout=20,
                    **extra,
                )
                break
            except groq.BadRequestError as exc:
                if not is_tool_use_failed(exc) or attempt == TOOL_CALL_RETRIES:
                    raise InfraError("sim_error") from exc
                self.retries += 1
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
        if invented_facts(text, self.spec.facts):
            raise InfraError("sim_invented_fact")
        return text
