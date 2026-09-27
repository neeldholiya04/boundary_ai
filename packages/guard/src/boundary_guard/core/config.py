from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from boundary_guard.core.types import Action, Execution, Mode, OnError, Stage

# Content at these stages is consumed by the model (or sent to a tool) immediately,
# so a check that runs after the fact cannot protect anything.
_BLOCKING_ONLY_STAGES = {Stage.TOOL_ARGS, Stage.TOOL_OUTPUT}


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
    stages: list[Stage] = Field(min_length=1)
    detector: DetectorConfig
    action: Action
    # Dataset labels this policy is scored against in evals (e.g. [injection]).
    detects: list[str] = Field(default_factory=list)
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

            policy.mode = policy.mode or self.defaults.mode
            policy.execution = policy.execution or self.defaults.execution
            policy.timeout_ms = policy.timeout_ms or self.defaults.timeout_ms
            policy.on_error = policy.on_error or self.defaults.on_error

            if policy.execution is Execution.ASYNC:
                if policy.action is not Action.FLAG:
                    raise ValueError(
                        f"policy {policy.id}: async policies can only flag, not {policy.action.value}"
                    )
                bad = _BLOCKING_ONLY_STAGES.intersection(policy.stages)
                if bad:
                    names = ", ".join(sorted(s.value for s in bad))
                    raise ValueError(f"policy {policy.id}: stages [{names}] require blocking execution")
        return self


def load_config(path: str | Path) -> GuardConfig:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return GuardConfig.model_validate(raw)
