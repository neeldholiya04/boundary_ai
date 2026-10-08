from __future__ import annotations

import hashlib
import random
import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from boundary_eval.fakes import GENERATORS


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
    # Fake credentials this scenario uses, name -> kind (see load_scenarios).
    fakes: dict[str, str] = Field(default_factory=dict)
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


_SECRET_REF = re.compile(r"\{\{secret:([a-z0-9_]+)\}\}")


def _fake_values(scenario_id: str, fakes: dict[str, str]) -> dict[str, str]:
    """Deterministic fake credentials for a scenario's `fakes:` block (name -> kind from fakes.py)."""
    values = {}
    for name, kind in fakes.items():
        if kind not in GENERATORS:
            raise ValueError(f"scenario {scenario_id}: unknown fake kind {kind!r}")
        seed = hashlib.sha256(f"{scenario_id}:{name}".encode()).digest()
        values[name] = GENERATORS[kind](random.Random(seed))
    return values


def _fill(obj: Any, values: dict[str, str], scenario_id: str) -> Any:
    """Replace `{{secret:name}}` everywhere in a scenario (task, tools, workspace, checks)."""
    if isinstance(obj, str):

        def sub(m: re.Match[str]) -> str:
            if m.group(1) not in values:
                raise ValueError(f"scenario {scenario_id}: {{{{secret:{m.group(1)}}}}} is not in its fakes")
            return values[m.group(1)]

        return _SECRET_REF.sub(sub, obj)
    if isinstance(obj, Keyed):
        return Keyed({k: _fill(v, values, scenario_id) for k, v in obj.items()})
    if isinstance(obj, dict):
        return {k: _fill(v, values, scenario_id) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, values, scenario_id) for v in obj]
    return obj


def load_scenarios(path: Path, fixture_base: Path) -> tuple[list[Scenario], dict[str, dict[str, Any]]]:
    """Returns (scenarios, resolved responses per scenario id). Fixture paths resolve from
    `fixture_base` (the repo root), so scenarios reuse the golden fixtures.

    A scenario's `fakes: {name: kind}` defines credentials it uses as `{{secret:name}}`, expanded the
    same way everywhere, so the files never hold a key-shaped string but checks can still look for it.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw.get("scenarios", [])
    scenarios: list[Scenario] = []
    resolved: dict[str, dict[str, Any]] = {}
    for item in items:
        values = _fake_values(str(item.get("id")), item.get("fakes") or {})
        scenario = Scenario.model_validate(_fill(item, values, str(item.get("id"))))
        scenarios.append(scenario)
        resolved[scenario.id] = {
            tool: _fill(_resolve(resp, fixture_base), values, scenario.id)
            for tool, resp in scenario.tools.items()
        }
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
