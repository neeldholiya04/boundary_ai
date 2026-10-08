from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from boundary_guard.core.types import Action, Execution, Mode, OnError, Stage

# Content at these stages is consumed by the model (or sent to a tool) immediately,
# so a check that runs after the fact cannot protect anything.
_BLOCKING_ONLY_STAGES = {Stage.TOOL_ARGS, Stage.TOOL_OUTPUT}
# The only stages a check knows which tool it is about, so the only ones a policy can scope to tools.
TOOL_STAGES = {Stage.TOOL_ARGS, Stage.TOOL_OUTPUT}


class DetectorConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str

    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_ms: int = Field(default=400, gt=0)
    on_error: OnError = OnError.FAIL_CLOSED
    mode: Mode = Mode.ENFORCE
    execution: Execution = Execution.BLOCKING


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str | None = None
    # What the end user is told when this policy stops something. Unset: the app's message for the
    # kind of policy (what it `detects`). Never the detector's reasons, which name scores and exemplars.
    message: str | None = Field(default=None, max_length=300)
    stages: list[Stage] = Field(min_length=1)
    detector: DetectorConfig
    action: Action
    # Dataset labels this policy is scored against in evals (e.g. [injection]).
    detects: list[str] = Field(default_factory=list)
    # Only check calls to / results from these tools (tool stages only). None = every tool.
    tools: list[str] | None = Field(default=None, min_length=1)
    # Unset fields inherit from `defaults`; resolved in GuardConfig.
    mode: Mode | None = None
    execution: Execution | None = None
    timeout_ms: int | None = Field(default=None, gt=0)
    on_error: OnError | None = None


class GuardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    defaults: Defaults = Field(default_factory=Defaults)
    policies: list[PolicyConfig]

    @model_validator(mode="after")
    def _resolve_and_validate(self) -> GuardConfig:
        seen: set[str] = set()
        for policy in self.policies:
            if policy.id in seen:
                raise ValueError(f"duplicate policy id: {policy.id}")
            seen.add(policy.id)
            resolve_policy(policy, self.defaults)
        return self


def resolve_policy(policy: PolicyConfig, defaults: Defaults) -> PolicyConfig:
    """Fill unset fields from `defaults` and check the rules every policy must follow. Used for the
    policy file and for policies added at runtime, so both are held to the same rules."""
    policy.mode = policy.mode or defaults.mode
    policy.execution = policy.execution or defaults.execution
    policy.timeout_ms = policy.timeout_ms or defaults.timeout_ms
    policy.on_error = policy.on_error or defaults.on_error

    if policy.execution is Execution.ASYNC:
        if policy.action is not Action.FLAG:
            raise ValueError(f"policy {policy.id}: async policies can only flag, not {policy.action.value}")
        bad = _BLOCKING_ONLY_STAGES.intersection(policy.stages)
        if bad:
            names = ", ".join(sorted(s.value for s in bad))
            raise ValueError(f"policy {policy.id}: stages [{names}] require blocking execution")
    if policy.tools is not None:
        outside = set(policy.stages) - TOOL_STAGES
        if outside:
            names = ", ".join(sorted(s.value for s in outside))
            raise ValueError(f"policy {policy.id}: tools can only scope tool stages, not [{names}]")
    return policy


def load_config(path: str | Path) -> GuardConfig:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return GuardConfig.model_validate(raw)
