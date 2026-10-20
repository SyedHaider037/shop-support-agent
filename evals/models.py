"""Eval task schema and the outcome of one run.

Tasks are plain JSON validated here, so a typo in a check name fails loudly at
load time instead of silently turning a check off.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.store.store import Store

Category = Literal[
    "products",
    "orders",
    "refunds",
    "leads",
    "scope",
    "attacks",
    "handoff",
    "false_positives",
]
Suite = Literal["regression", "capability"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoryMessage(_Strict):
    """A message the client claims was sent earlier (it may be forged)."""

    role: Literal["user", "assistant"]
    content: str


class CustomerSpec(_Strict):
    """A simulated customer: who they are, what they may say, what they want."""

    persona: str
    facts: dict[str, str]
    goal: str
    max_turns: int = Field(default=5, ge=1, le=8)


class RefundSetup(_Strict):
    order_id: str
    email: str
    reason: str


class Setup(_Strict):
    """Store state before the run, e.g. a refund that is already pending."""

    refund_requests: list[RefundSetup] = []


class ExpectedCall(_Strict):
    name: str
    # Each given argument must equal the call's (case-insensitive, emails compared masked).
    args: dict[str, str] = {}
    # Expected result: "ok" or an error code such as "no_matching_order".
    result: str | None = None


class StateChecks(_Strict):
    pending_refunds: dict[str, int] = {}  # order_id -> pending approvals for it
    refund_status: dict[str, Literal["refunded"] | None] = {}  # order_id -> expected status
    approvals_total: int | None = None  # approvals created during the run (setup excluded)
    approved_count: int | None = None  # approvals in status "approved"
    leads_count: int | None = None
    lead_emails: list[str] | None = None
    audit_actors_allowed: list[Literal["agent", "admin"]] | None = None


class ToolChecks(_Strict):
    must_call: list[ExpectedCall] = []
    must_not_call: list[str] = []
    max_calls: int | None = None  # all tools together
    max_calls_by_tool: dict[str, int] = {}


class ReplyChecks(_Strict):
    # At least one regex must match. "final": the last agent reply; "any": any agent reply
    # (used for simulated customers, whose last turn may be small talk).
    must_match_any: list[str] = []
    match_scope: Literal["final", "any"] = "final"
    # No regex may match ANY agent reply in the conversation.
    must_not_match: list[str] = []
    # No email or phone number from the sample customers unless the user typed it.
    no_other_customer_contacts: bool = True
    # Every $ amount in the replies must be a real catalog price or order total.
    only_known_prices: bool = False


class GuardrailChecks(_Strict):
    input_rule: str | None = None  # this input rule must fire
    no_model_call: bool = False
    not_refused: bool = False  # no input guardrail may fire (false-positive tasks)


class Checks(_Strict):
    state: StateChecks | None = None
    tools: ToolChecks | None = None
    reply: ReplyChecks | None = None
    guardrail: GuardrailChecks | None = None
    max_steps: int | None = None  # model calls across the whole conversation
    # Agent-loop errors this task expects. Any other one fails the task (agent_error), so a
    # fallback reply after an internal error can never pass as a success.
    expected_agent_errors: list[str] = []

    def count(self) -> int:
        """How many individual checks this task runs, for reporting."""
        total = 0
        if self.state:
            s = self.state
            total += len(s.pending_refunds) + len(s.refund_status)
            total += sum(
                v is not None
                for v in (
                    s.approvals_total,
                    s.approved_count,
                    s.leads_count,
                    s.lead_emails,
                    s.audit_actors_allowed,
                )
            )
        if self.tools:
            t = self.tools
            total += len(t.must_call) + len(t.must_not_call) + len(t.max_calls_by_tool)
            total += t.max_calls is not None
        if self.reply:
            r = self.reply
            total += bool(r.must_match_any) + len(r.must_not_match)
            total += r.no_other_customer_contacts + r.only_known_prices
        if self.guardrail:
            g = self.guardrail
            total += (g.input_rule is not None) + g.no_model_call + g.not_refused
        return total + (self.max_steps is not None)


class Task(_Strict):
    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    category: Category
    suite: Suite
    description: str
    history: list[HistoryMessage] = []
    turns: list[str] = []
    customer: CustomerSpec | None = None
    setup: Setup = Setup()
    checks: Checks
    notes: str

    @model_validator(mode="after")
    def _one_driver(self) -> "Task":
        if bool(self.turns) == (self.customer is not None):
            raise ValueError(f"{self.id}: give exactly one of 'turns' or 'customer'")
        if self.checks.count() == 0:
            raise ValueError(f"{self.id}: a task needs at least one check")
        return self

    @property
    def simulated(self) -> bool:
        return self.customer is not None


@dataclass(frozen=True)
class ToolCallRecord:
    name: str
    arguments: Any  # email-masked, as the agent loop records them
    result: str  # "ok" or the error code


@dataclass
class Outcome:
    """Everything a grader may look at after one conversation."""

    store: Store
    replies: list[str] = field(default_factory=list)  # agent replies, unmasked
    user_texts: list[str] = field(default_factory=list)  # everything the user side typed
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    input_rules_fired: list[str] = field(default_factory=list)
    model_calls: int = 0
    approvals_before: int = 0  # approvals created by setup, not by the agent
    agent_errors: list[str] = field(default_factory=list)  # e.g. tool_call_failed, max_steps


TASKS_PATH = Path(__file__).resolve().parent / "tasks.json"


def load_tasks(path: Path = TASKS_PATH) -> list[Task]:
    """Load and validate the task file; duplicate ids are an error, not a silent overwrite."""
    tasks = TypeAdapter(list[Task]).validate_json(path.read_bytes())
    ids = [t.id for t in tasks]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate task ids: {duplicates}")
    return tasks
