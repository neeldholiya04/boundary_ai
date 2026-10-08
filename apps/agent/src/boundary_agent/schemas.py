from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


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


PolicyRuleType = Literal[
    "block_tool", "require_approval", "validate_args", "token_budget", "cost_budget", "guard_signal"
]
PolicyMode = Literal["off", "shadow", "enforce"]


def check_policy_shape(
    rule_type: str | None, conditions: dict[str, Any] | None, action: dict[str, Any] | None
) -> None:
    """Reject conditions the engine would silently ignore (a budget with no limit, a folder rule
    with no folders), so a policy that looks active always does something."""
    conditions = conditions or {}
    action = action or {}
    if rule_type == "validate_args":
        prefixes = conditions.get("allow_prefixes")
        if conditions.get("path_arg") and not (isinstance(prefixes, list) and prefixes):
            raise ValueError("validate_args needs at least one folder in allow_prefixes")
    if rule_type == "token_budget":
        limit = conditions.get("max_tokens")
        if isinstance(limit, bool) or not isinstance(limit, int | float) or limit <= 0:
            raise ValueError("token_budget needs max_tokens above zero")
    if rule_type == "cost_budget":
        limit = conditions.get("max_cost")
        if isinstance(limit, bool) or not isinstance(limit, int | float) or limit <= 0:
            raise ValueError("cost_budget needs max_cost above zero")
    if rule_type == "guard_signal" and action.get("verdict", "require_approval") not in (
        "require_approval",
        "block",
    ):
        raise ValueError("guard_signal verdict must be require_approval or block")


class PolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    rule_type: PolicyRuleType
    enabled: bool = True
    # The API default keeps scripts that predate modes working; the dashboard creates in shadow.
    mode: PolicyMode = "enforce"
    priority: int = 100
    target_tool: str | None = None
    target_server_id: str | None = None
    conditions: dict[str, Any] | None = None
    action: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _shape(self) -> PolicyCreate:
        check_policy_shape(self.rule_type, self.conditions, self.action)
        return self


class PolicyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    rule_type: PolicyRuleType | None = None
    enabled: bool | None = None
    mode: PolicyMode | None = None
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


class GuardRuleTestRequest(BaseModel):
    spec: dict[str, Any]
    # Also check the eval set's benign records at the rule's stages (judge rules: a small sample).
    sample_benign: bool = True


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
