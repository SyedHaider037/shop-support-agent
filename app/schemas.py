"""Request and response bodies for /chat.

The server keeps no memory between requests (serverless), so the client sends
the whole history each time. These limits keep that untrusted history small
and stop a client from injecting system or tool messages.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_MESSAGES = 20
MAX_CONTENT_CHARS = 1000


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)

    @model_validator(mode="after")
    def _last_message_is_from_user(self) -> "ChatRequest":
        if self.messages[-1].role != "user":
            raise ValueError("the last message must have role 'user'")
        return self


class StepOut(BaseModel):
    tool: str
    arguments: Any
    result: dict[str, Any]
    latency_ms: int


class UsageOut(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model_calls: int


class GuardrailEventOut(BaseModel):
    layer: str
    rule: str
    action: str


class AdminApproval(BaseModel):
    id: str
    order_id: str
    amount: float
    reason: str
    status: str
    created_at: datetime
    decided_at: datetime | None
    email: str  # masked


class ChatResponse(BaseModel):
    reply: str
    steps: list[StepOut]
    usage: UsageOut
    guardrail_events: list[GuardrailEventOut]
    trace: dict[str, Any]
