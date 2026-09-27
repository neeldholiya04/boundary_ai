from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ToolDescriptor:
    server_id: str
    server_name: str
    transport: str
    name: str
    description: str | None
    input_schema: dict[str, Any] | None


@dataclass(slots=True)
class ToolCall:
    server_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlannerDecision:
    assistant_message: str | None
    tool_call: ToolCall | None = None
    usage_tokens: int = 0
    usage_cost: float = 0.0


@dataclass(slots=True)
class PlannerMessage:
    role: str
    content: str


@dataclass(slots=True)
class ToolExecutionIntent:
    conversation_id: str
    run_id: str
    server_id: str
    server_name: str
    tool_name: str
    arguments: dict[str, Any]
    token_budget: int | None
    cost_budget: float | None
    spent_tokens: int
    spent_cost: float
    # Guard signals for `guard_signal` policy rules.
    run_tainted: bool = False
    taint_reason: str | None = None


@dataclass(slots=True)
class PolicyDecision:
    verdict: str
    reason: str
    matched_rule_ids: list[str]
    requires_approval: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(slots=True)
class ExecutedToolStep:
    tool_call: ToolCall
    result: dict[str, Any]
    is_error: bool = False
