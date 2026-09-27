from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Keyed(dict):
    """A tool response chosen by the call's primary argument (e.g. a URL). Distinct from a plain
    dict, which is returned as structured content whatever the argument."""


class Kind(StrEnum):
    ATTACK = "attack"
    BENIGN = "benign"


class Split(StrEnum):
    DEV = "dev"
    TEST = "test"


class Check(BaseModel):
    """One predicate over a finished run. Exactly one primary key is set."""

    model_config = ConfigDict(extra="forbid")

    tool_called: str | None = None
    tool_not_called: str | None = None
    args_contains: dict[str, str] = Field(default_factory=dict)
    any_tool_args_contains: str | None = None
    final_contains: str | None = None
    final_not_contains: str | None = None

    @model_validator(mode="after")
    def _one_primary(self) -> Check:
        primaries = [
            self.tool_called,
            self.tool_not_called,
            self.any_tool_args_contains,
            self.final_contains,
            self.final_not_contains,
        ]
        if sum(x is not None for x in primaries) != 1:
            raise ValueError(
                "a check needs exactly one of: tool_called, tool_not_called, "
                "any_tool_args_contains, final_contains, final_not_contains"
            )
        if self.args_contains and self.tool_called is None:
            raise ValueError("args_contains requires tool_called")
        return self


class ToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture: str | None = None
    text: str | None = None
    json_value: Any = None

    @model_validator(mode="after")
    def _one_source(self) -> ToolResponse:
        if sum(x is not None for x in (self.fixture, self.text, self.json_value)) != 1:
            raise ValueError("a tool response needs exactly one of: fixture, text, json_value")
        return self


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    split: Split
    kind: Kind
    user_task: str = Field(min_length=1)
    response_schema: str | None = None
    # tool name -> a single ToolResponse, or {key: ToolResponse} keyed by the primary argument.
    tools: dict[str, ToolResponse | dict[str, ToolResponse]] = Field(default_factory=dict)
    workspace: dict[str, str] = Field(default_factory=dict)
    canary: str | None = None
    notes: str | None = None
    attack_success: list[Check] = Field(default_factory=list)
    task_success: list[Check] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kind_rules(self) -> Scenario:
        if self.kind is Kind.ATTACK and not self.attack_success:
            raise ValueError("attack scenarios need at least one attack_success check")
        if self.kind is Kind.BENIGN and self.attack_success:
            raise ValueError("benign scenarios must not define attack_success checks")
        return self


def _resolve(responses: ToolResponse | dict[str, ToolResponse], base: Path) -> Any:
    def one(r: ToolResponse) -> Any:
        if r.fixture is not None:
            return (base / r.fixture).read_text(encoding="utf-8")
        if r.text is not None:
            return r.text
        return r.json_value

    if isinstance(responses, ToolResponse):
        return one(responses)
    return Keyed({key: one(r) for key, r in responses.items()})


def load_scenarios(path: Path, fixture_base: Path) -> tuple[list[Scenario], dict[str, dict[str, Any]]]:
    """Returns (scenarios, resolved responses per scenario id). Fixture paths resolve from
    `fixture_base` (the repo root), so scenarios reuse the golden fixtures."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw.get("scenarios", [])
    scenarios: list[Scenario] = []
    resolved: dict[str, dict[str, Any]] = {}
    for item in items:
        scenario = Scenario.model_validate(item)
        scenarios.append(scenario)
        resolved[scenario.id] = {tool: _resolve(resp, fixture_base) for tool, resp in scenario.tools.items()}
    return scenarios, resolved


def load_dir(directory: Path, fixture_base: Path) -> tuple[list[Scenario], dict[str, dict[str, Any]]]:
    all_scenarios: list[Scenario] = []
    all_resolved: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for path in sorted(directory.rglob("*.yaml")):
        scenarios, resolved = load_scenarios(path, fixture_base)
        for scenario in scenarios:
            if scenario.id in seen:
                raise ValueError(f"duplicate scenario id: {scenario.id}")
            seen.add(scenario.id)
        all_scenarios.extend(scenarios)
        all_resolved.update(resolved)
    return all_scenarios, all_resolved
