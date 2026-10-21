from typing import Any

import pytest

from app.config import Settings
from evals.customer_sim import END, OPENING, SimulatedCustomer, system_prompt
from evals.models import CustomerSpec, Task
from evals.pacer import Pacer
from evals.run import user_factory
from evals.runner import InfraError, RunConfig, run_task
from tests.fakes import (
    FakeClock,
    FakeGroq,
    connection_error,
    rate_limit_error,
    reply,
    tool_call,
    tool_calls,
)

SPEC = CustomerSpec(
    persona="Sam, a busy customer waiting for a backpack.",
    facts={"order ID": "ORD-1002", "email": "sam.lee@example.com"},
    goal="Find out where the order is.",
    max_turns=4,
)


def test_prompt_has_persona_goal_facts_and_rules() -> None:
    prompt = system_prompt(SPEC)

    assert "Sam, a busy customer" in prompt
    assert "Find out where the order is." in prompt
    assert "- order ID: ORD-1002" in prompt
    assert "ONLY when the assistant asks" in prompt
    assert "Never invent facts" in prompt
    assert "one or two short sentences" in prompt
    assert END in prompt


def test_roles_are_swapped_and_the_chat_is_opened() -> None:
    fake = FakeGroq(reply("I want to know where my order is."))
    sim = SimulatedCustomer(SPEC, fake, "openai/gpt-oss-20b")
    history = [
        {"role": "user", "content": "Hi, where's my order?"},
        {"role": "assistant", "content": "What's your order ID?"},
    ]

    sim.next_message(history)

    sent = fake.requests[0]["messages"]
    assert sent[0]["role"] == "system"
    assert sent[1] == {"role": "user", "content": OPENING}
    assert sent[2] == {"role": "assistant", "content": "Hi, where's my order?"}
    assert sent[3] == {"role": "user", "content": "What's your order ID?"}
    assert fake.requests[0]["model"] == "openai/gpt-oss-20b"
    assert fake.requests[0]["reasoning_effort"] == "low"


def test_reasoning_effort_only_for_gpt_oss() -> None:
    fake = FakeGroq(reply("hello"))

    SimulatedCustomer(SPEC, fake, "qwen/qwen3.8-27b").next_message([])

    assert "reasoning_effort" not in fake.requests[0]


@pytest.mark.parametrize("text", [END, f"Thanks, that's all. {END}", f"  {END}\n"])
def test_end_conversation_stops(text: str) -> None:
    assert SimulatedCustomer(SPEC, FakeGroq(reply(text)), "m").next_message([]) is None


@pytest.mark.parametrize(
    ("error", "code", "retry_after"),
    [
        (rate_limit_error("12"), "sim_busy", 12.0),
        (connection_error(), "sim_error", None),
        (None, "sim_empty", None),
    ],
)
def test_simulator_failures_are_infra_errors(
    error: Exception | None, code: str, retry_after: float | None
) -> None:
    fake = FakeGroq(error if error is not None else reply("   "))

    with pytest.raises(InfraError) as caught:
        SimulatedCustomer(SPEC, fake, "m").next_message([])

    assert (caught.value.code, caught.value.retry_after_s) == (code, retry_after)


# --- a full simulated conversation through the runner -----------------------------


def sim_task(**overrides: Any) -> Task:
    data: dict[str, Any] = {
        "id": "order-sim-01",
        "category": "orders",
        "suite": "capability",
        "description": "agent must ask for the order id and email",
        "customer": SPEC.model_dump(),
        "checks": {
            "tools": {
                "must_call": [
                    {
                        "name": "get_order_status",
                        "args": {"order_id": "ORD-1002", "email": "sam.lee@example.com"},
                        "result": "ok",
                    }
                ]
            },
            "reply": {"must_match_any": ["shipped"], "match_scope": "any"},
        },
        "notes": "test",
    }
    data.update(overrides)
    return Task.model_validate(data)


class Harness:
    def __init__(self, agent_script: list[Any], sim_script: list[Any]) -> None:
        self.clock = FakeClock()
        self.slept: list[float] = []
        self.agent_fake = FakeGroq(*agent_script)
        self.sim_fake = FakeGroq(*sim_script)
        self.agent = Pacer(self.agent_fake, clock=self.clock, sleep=self.sleep)
        self.sim = Pacer(self.sim_fake, clock=self.clock, sleep=self.sleep)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.advance(seconds)

    def run(self, task: Task) -> dict[str, Any]:
        config = RunConfig(Settings(groq_api_key="k"), commit="c", sim_model="openai/gpt-oss-20b")
        make_user = user_factory(self.sim, "openai/gpt-oss-20b")
        return run_task(task, 1, config, self.agent, make_user, self.sim, self.clock, self.sleep)


ORDER = tool_call("get_order_status", {"order_id": "ORD-1002", "email": "sam.lee@example.com"})


def test_simulated_conversation_passes_and_is_recorded() -> None:
    h = Harness(
        agent_script=[
            reply("Sure, what's your order ID and email?"),
            tool_calls(ORDER),
            reply("Your order ORD-1002 has shipped."),
        ],
        sim_script=[
            reply("Hi, where is my order?"),
            reply("ORD-1002, sam.lee@example.com"),
            reply(END),
        ],
    )

    row = h.run(sim_task())

    assert row["status"] == "pass", row["failed_checks"]
    assert row["simulated"] is True
    assert [m["role"] for m in row["transcript"]] == ["user", "assistant"] * 2
    assert row["transcript"][2]["content"] == "ORD-1002, s***@example.com"
    assert row["sim_tokens"]["prompt"] == 30  # three simulator calls of 10 prompt tokens
    assert len(h.sim_fake.requests) == 3


def test_max_turns_caps_a_customer_who_never_ends() -> None:
    h = Harness(
        agent_script=[reply("Could you tell me more?") for _ in range(2)],
        sim_script=[reply("Hello?") for _ in range(5)],
    )

    row = h.run(sim_task(customer={**SPEC.model_dump(), "max_turns": 2}))

    assert len(h.agent_fake.requests) == 2
    assert len(h.sim_fake.requests) == 2  # no third customer message after the cap
    assert row["status"] == "fail"


def test_simulator_failure_is_infra_error_not_fail() -> None:
    h = Harness(agent_script=[], sim_script=[connection_error()] * 3)

    row = h.run(sim_task())

    assert row["status"] == "infra_error"
    assert row["error"] == "sim_error"
    assert h.agent_fake.requests == []


# --- fairness: the simulator may not invent facts -----------------------------------


@pytest.mark.parametrize(
    ("text", "invented"),
    [
        ("My order is ORD-1002 and my email is sam.lee@example.com", []),
        ("It's ord\u20111002, SAM.LEE@example.com", []),
        ("Try ORD-1005 instead", ["ORD-1005"]),
        ("My email is sam@example.com", ["sam@example.com"]),
        ("Call me on 555-123-4567", ["555-123-4567"]),
        ("It shipped on 2026-10-16, right?", []),  # a date the agent said is not a fact claim
    ],
)
def test_invented_facts(text: str, invented: list[str]) -> None:
    from evals.customer_sim import invented_facts

    assert invented_facts(text, SPEC.facts) == invented


def test_an_invented_fact_is_an_infra_error() -> None:
    sim = SimulatedCustomer(SPEC, FakeGroq(reply("Oh, it's ORD-1006 actually.")), "m")

    with pytest.raises(InfraError) as caught:
        sim.next_message([])

    assert caught.value.code == "sim_invented_fact"


def test_invented_fact_run_is_infra_error_and_shown_in_the_summary() -> None:
    from evals.metrics import markdown_summary

    h = Harness(agent_script=[], sim_script=[reply("My email is fake.person@example.com")] * 3)

    row = h.run(sim_task())

    assert (row["status"], row["error"]) == ("infra_error", "sim_invented_fact")
    assert h.agent_fake.requests == []
    summary = markdown_summary([row], k=1)
    assert "`order-sim-01`: sim_invented_fact x1" in summary


# --- Phase 4C fix 5 ------------------------------------------------------------------


def test_a_stray_simulator_tool_call_is_retried_and_counted() -> None:
    from tests.fakes import tool_use_failed_error

    sim = SimulatedCustomer(SPEC, FakeGroq(tool_use_failed_error(), reply("Hi there")), "m")

    assert sim.next_message([]) == "Hi there"
    assert sim.retries == 1
    assert "Never call a tool." in system_prompt(SPEC)


def test_repeated_simulator_tool_calls_end_as_sim_error() -> None:
    from tests.fakes import tool_use_failed_error

    sim = SimulatedCustomer(SPEC, FakeGroq(*[tool_use_failed_error()] * 3), "m")

    with pytest.raises(InfraError) as caught:
        sim.next_message([])
    assert caught.value.code == "sim_error"


def test_an_empty_simulated_chat_is_an_infra_error_not_a_fail() -> None:
    # Before this fix the row was graded "fail" with 0 agent turns (5 times in the first run).
    h = Harness(agent_script=[], sim_script=[reply(END)] * 3)

    row = h.run(sim_task())

    assert (row["status"], row["error"]) == ("infra_error", "sim_empty_conversation")
    assert h.agent_fake.requests == []


def test_same_model_agent_and_simulator_share_one_pacer() -> None:
    from evals.run import make_pacers

    task = sim_task()
    agent, sim = make_pacers(object(), "openai/gpt-oss-20b", "openai/gpt-oss-20b", [(task, 1)])
    other_agent, other_sim = make_pacers(object(), "m1", "m2", [(task, 1)])

    assert sim is not None and agent.pacer is sim.pacer
    assert agent.stats is not sim.stats
    assert other_sim is not None and other_agent.pacer is not other_sim.pacer


# --- a customer that ends while the agent waits for an answer (Phase 4E, P3) -------------


@pytest.mark.parametrize(
    ("text", "waits"),
    [
        ("Could you please provide your order ID and email?", True),
        ("Would you like me to save your name and email so our team can contact you?", True),
        ("Thanks! What's the reason?Sure, I'll pass it on.", True),
        ("Your order has shipped. Is there anything else I can help you with?", False),
        ("Your order has shipped.", False),
    ],
)
def test_waits_for_answer(text: str, waits: bool) -> None:
    from evals.runner import waits_for_answer

    assert waits_for_answer(text) is waits


def test_a_customer_ending_after_a_question_is_asked_again_and_continues() -> None:
    h = Harness(
        agent_script=[
            reply("Sure, what's your order ID and email?"),
            tool_calls(ORDER),
            reply("Your order ORD-1002 has shipped."),
        ],
        sim_script=[
            reply("Hi, where is my order?"),
            reply(END),  # ends although the agent asked a question
            reply("ORD-1002, sam.lee@example.com"),
            reply(END),
        ],
    )

    row = h.run(sim_task())

    assert row["status"] == "pass", row["failed_checks"]
    assert row["sim_early_end_retries"] == 1
    assert sum(m["role"] == "user" for m in row["transcript"]) == 2


def test_a_customer_that_keeps_ending_after_a_question_is_an_infra_error_not_a_fail() -> None:
    h = Harness(
        agent_script=[reply("Sure, what's your order ID and email?")],
        sim_script=[reply("Hi, where is my order?"), reply(END), reply(END), reply(END)],
    )

    row = h.run(sim_task())

    assert (row["status"], row["error"]) == ("infra_error", "sim_ended_early")
    assert len(h.sim_fake.requests) == 4  # opening, then the end plus 2 retries of that turn
    assert len(h.agent_fake.requests) == 1  # the whole trial is not run again
    assert len(row["attempts"]) == 1


def test_a_customer_whose_goal_is_reached_ends_without_retries() -> None:
    h = Harness(
        agent_script=[
            reply("Sure, what's your order ID and email?"),
            tool_calls(ORDER),
            reply("Your order ORD-1002 has shipped. Is there anything else I can help with?"),
        ],
        sim_script=[
            reply("Hi, where is my order?"),
            reply("ORD-1002, sam.lee@example.com"),
            reply(END),
        ],
    )

    row = h.run(sim_task())

    assert row["status"] == "pass", row["failed_checks"]
    assert row["sim_early_end_retries"] == 0
    assert len(h.sim_fake.requests) == 3


def test_a_customer_that_answers_is_never_asked_again() -> None:
    h = Harness(
        agent_script=[
            reply("What's your order ID and email?"),
            tool_calls(ORDER),
            reply("It has shipped."),
        ],
        sim_script=[
            reply("Where is my order?"),
            reply("ORD-1002, sam.lee@example.com"),
            reply(END),
        ],
    )

    row = h.run(sim_task())

    assert row["sim_early_end_retries"] == 0
    assert row["transcript"][2]["content"] == "ORD-1002, s***@example.com"


def test_max_turns_still_ends_a_chat_whose_agent_keeps_asking() -> None:
    h = Harness(
        agent_script=[reply("Could you tell me more?") for _ in range(2)],
        sim_script=[reply("Hello?") for _ in range(5)],
    )

    row = h.run(sim_task(customer={**SPEC.model_dump(), "max_turns": 2}))

    assert row["status"] == "fail"  # graded on what happened, not an infra error
    assert row["sim_early_end_retries"] == 0
    assert len(h.sim_fake.requests) == 2


def test_the_prompt_tells_the_customer_to_answer_questions_before_ending() -> None:
    prompt = " ".join(system_prompt(SPEC).split())

    assert "A question is never the end of the chat." in prompt
    assert "Never end while the assistant is waiting for an answer from you." in prompt
