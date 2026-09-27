from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Stage(StrEnum):
    """Where in the agent loop a check runs."""

    USER_INPUT = "user_input"
    TOOL_ARGS = "tool_args"
    TOOL_OUTPUT = "tool_output"
    FINAL_OUTPUT = "final_output"


class Action(StrEnum):
    ALLOW = "allow"
    FLAG = "flag"
    REDACT = "redact"
    ESCALATE = "escalate"
    BLOCK = "block"


# Aggregation precedence: the strongest enforced action wins.
ACTION_WEIGHT: dict[Action, int] = {
    Action.ALLOW: 0,
    Action.FLAG: 1,
    Action.REDACT: 2,
    Action.ESCALATE: 3,
    Action.BLOCK: 4,
}


class Mode(StrEnum):
    ENFORCE = "enforce"
    SHADOW = "shadow"
    OFF = "off"


class Execution(StrEnum):
    BLOCKING = "blocking"
    ASYNC = "async"


class OnError(StrEnum):
    FAIL_CLOSED = "fail_closed"
    FAIL_OPEN = "fail_open"


def strongest(actions: list[Action]) -> Action:
    if not actions:
        return Action.ALLOW
    return max(actions, key=ACTION_WEIGHT.__getitem__)


@dataclass(slots=True, frozen=True)
class Span:
    """A matched region of the checked text, in character offsets [start, end)."""

    start: int
    end: int
    label: str


@dataclass(slots=True)
class CheckContext:
    """Caller-supplied context. Detectors may read it; the pipeline never mutates it."""

    run_id: str | None = None
    conversation_id: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    source: str | None = None
    # Schema id the caller expects the final output to follow (e.g. "research_note.v1").
    response_schema: str | None = None
    # Trusted reference texts (e.g. tool outputs) for groundedness checks.
    references: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Detection:
    """What a detector returns. The pipeline turns this into a PolicyDecision."""

    triggered: bool
    score: float | None = None
    spans: list[Span] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    # Transform detectors (e.g. schema repair) return rewritten text here.
    rewrite: str | None = None


@dataclass(slots=True)
class PolicyDecision:
    policy_id: str
    policy_version: str
    stage: Stage
    mode: Mode
    execution: Execution
    # Action actually applied (always ALLOW in shadow mode).
    action: Action
    # Action the policy would apply if enforced; equals `action` in enforce mode.
    would_action: Action
    detector: str
    score: float | None = None
    threshold: float | None = None
    reasons: list[str] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    rewritten: bool = False
    error: str | None = None


@dataclass(slots=True)
class GuardResult:
    stage: Stage
    # Strongest enforced action across blocking policies.
    action: Action
    # Strongest action any active policy would take if every one were enforced.
    would_action: Action
    # Text after enforced transforms and redactions. On BLOCK/ESCALATE the caller decides what to do.
    text: str
    decisions: list[PolicyDecision]
    config_hash: str
    # Wall-clock time of the blocking part of the check (policies run concurrently).
    latency_ms: float = 0.0
    # Async policies scheduled for this check; their decisions go to sinks, not here.
    pending_async: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.action is Action.BLOCK

    @property
    def cost_usd(self) -> float:
        return sum(d.cost_usd for d in self.decisions)

    def decision(self, policy_id: str) -> PolicyDecision | None:
        return next((d for d in self.decisions if d.policy_id == policy_id), None)
