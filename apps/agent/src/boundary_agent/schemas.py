from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    title: str | None = None
    token_budget: int | None = None
    cost_budget: float | None = None


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1)
    # Ask for the final answer in a schema the guard validates (e.g. "research_note.v1").
    response_schema: str | None = None


class ChatResponse(BaseModel):
    conversation_id: str
    run_id: str
    status: str
    assistant_message: str
    tool_call: dict[str, Any] | None = None
    executed_tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    approval_request_id: str | None = None
    trace_url: str | None = None  # Langfuse trace for this run, when tracing is on
    # What the guard changed in the user's own message (e.g. a pasted key replaced by a placeholder).
    guard_notices: list[str] = Field(default_factory=list)


class MCPServerCreate(BaseModel):
    name: str
    transport: Literal["stdio", "sse", "streamable_http"]
    enabled: bool = True
    config: dict[str, Any]


class MCPServerUpdate(BaseModel):
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class PolicyCreate(BaseModel):
    name: str = Field(min_length=1)
    rule_type: Literal[
        "block_tool", "require_approval", "validate_args", "token_budget", "cost_budget", "guard_signal"
    ]
    enabled: bool = True
    priority: int = 100
    target_tool: str | None = None
    target_server_id: str | None = None
    conditions: dict[str, Any] | None = None
    action: dict[str, Any] | None = None


class PolicyUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    target_tool: str | None = None
    target_server_id: str | None = None
    conditions: dict[str, Any] | None = None
    action: dict[str, Any] | None = None


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approved", "denied"]
    comment: str | None = None


class GuardModeRequest(BaseModel):
    mode: Literal["enforce", "shadow", "off"]


class EventPayload(BaseModel):
    type: str
    payload: dict[str, Any]


class ToolSummary(BaseModel):
    id: str
    server_id: str
    server_name: str
    name: str
    description: str | None
    input_schema: dict[str, Any] | None
    discovered_at: datetime
