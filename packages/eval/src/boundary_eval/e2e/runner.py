from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boundary_agent.agent import AgentRuntime
from boundary_agent.audit import AuditLogger
from boundary_agent.config import Settings
from boundary_agent.db import Base
from boundary_agent.guarding import GuardAdapter
from boundary_agent.llm import get_planner
from boundary_agent.models import GuardDecision, MCPServer, Policy
from boundary_agent.policy import PolicyEngine
from boundary_agent.realtime import EventBroker
from boundary_agent.telemetry import DISABLED, Telemetry
from boundary_eval.e2e.checks import Trajectory, attack_succeeded, task_succeeded
from boundary_eval.e2e.fixture_mcp import FixtureMCP
from boundary_eval.e2e.scenarios import Kind, Scenario
from boundary_eval.runner import _NO_TIMEOUT_MS
from boundary_guard import Guard, Mode


@dataclass(slots=True)
class Config:
    """A defence configuration to run every scenario under."""

    name: str
    guard: bool = True  # run the guard at all
    mode: Mode | None = None  # force all policies to this mode; None = use the YAML modes
    spotlight: bool = True
    taint: bool = True  # seed the tainted-run -> approval rules
    on_block: str = "continue"


CONFIGS: dict[str, Config] = {
    "no_defense": Config("no_defense", guard=False, spotlight=False, taint=False),
    "spotlight_only": Config("spotlight_only", guard=False, spotlight=True, taint=False),
    "filters": Config("filters", guard=True, spotlight=False, taint=False),
    "filters_spotlight": Config("filters_spotlight", guard=True, spotlight=True, taint=False),
    "filters_taint": Config("filters_taint", guard=True, spotlight=True, taint=True),
    "shadow": Config("shadow", guard=True, mode=Mode.SHADOW, spotlight=True, taint=True),
    # Every policy enforced, the tool-output injection detectors included (blocked output is withheld
    # from the model): the "what if we enforced everything" comparison point.
    "enforce": Config("enforce", guard=True, mode=Mode.ENFORCE, spotlight=True, taint=True),
}

# The guard_signal rules main.py seeds; the runner adds them for `taint` configs.
_TAINT_RULES = [
    ("write_file", "Tainted run: writes need approval."),
    ("delete_file", "Tainted run: deletes need approval."),
    ("send_email", "Tainted run: sending needs approval."),
]


@dataclass(slots=True)
class ScenarioResult:
    scenario_id: str
    split: str
    kind: str
    config: str
    status: str
    steps: int
    cost_usd: float
    attack_success: bool
    attack_hits: list[str]
    task_success: bool
    task_failures: list[str]
    final_message: str
    run_id: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # [{tool, arguments}] in call order
    guard_flags: list[dict[str, str]] = field(default_factory=list)  # non-allow decisions (shadow included)


@dataclass(slots=True)
class E2ERun:
    results: list[ScenarioResult] = field(default_factory=list)


def _build_guard(policy_path: Path, config: Config, *, enforce_timeouts: bool = False) -> Guard | None:
    if not config.guard:
        return None
    guard = Guard.from_yaml(policy_path)
    if not enforce_timeouts:
        # Same rule as the detector eval: a verdict must not depend on how fast (or how short of
        # memory) the machine is. Timeout behaviour is measured by the load test instead.
        for policy in guard.config.policies:
            policy.timeout_ms = _NO_TIMEOUT_MS
    if config.mode is not None:
        for pid in guard.policy_ids:
            guard.set_mode(pid, config.mode)
    return guard


async def run_scenario(
    scenario: Scenario,
    responses: dict[str, Any],
    config: Config,
    *,
    policy_path: Path,
    planner: Any | None = None,
    settings_overrides: dict[str, Any] | None = None,
    enforce_timeouts: bool = False,
    planner_factory: Callable[[Settings], Any] | None = None,
    telemetry: Telemetry = DISABLED,
) -> ScenarioResult:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            server = MCPServer(name="fixture", transport="stdio", enabled=True, config_json={})
            session.add(server)
            await session.flush()
            if config.taint:
                for tool, reason in _TAINT_RULES:
                    session.add(
                        Policy(
                            name=f"Tainted run: {tool}",
                            rule_type="guard_signal",
                            enabled=True,
                            priority=190,
                            target_tool=tool,
                            conditions_json={"run_tainted": True},
                            action_json={"verdict": "require_approval", "reason": reason},
                        )
                    )
            await session.flush()

            mcp = FixtureMCP(server.id, server.name, responses, workspace=dict(scenario.workspace))
            broker = EventBroker(None)
            audit = AuditLogger(broker)
            guard = _build_guard(policy_path, config, enforce_timeouts=enforce_timeouts)
            adapter = GuardAdapter(guard, audit, telemetry=telemetry) if guard else None

            base_settings = {
                "llm_provider": "litellm",
                "guard_spotlight": config.spotlight,
                "guard_tool_output_on_block": config.on_block,
                "max_tool_steps": 8,
                **(settings_overrides or {}),
            }
            settings = Settings(**base_settings)
            if planner is not None:
                run_planner = planner
            elif planner_factory is not None:
                run_planner = planner_factory(settings)  # per config: spotlight etc. come from `settings`
            else:
                run_planner = get_planner(settings)
            runtime = AgentRuntime(
                settings, run_planner, mcp, PolicyEngine(), audit, guard=adapter, telemetry=telemetry
            )

            response = await runtime.handle_chat(
                session, scenario.user_task, None, response_schema=scenario.response_schema
            )
            if guard is not None:
                await guard.drain()

            traj = Trajectory(mcp=mcp, final_message=response.assistant_message, status=response.status)
            cost = await _conversation_cost(session, response.conversation_id)
            flagged = (
                await session.scalars(
                    select(GuardDecision).where(
                        GuardDecision.run_id == response.run_id, GuardDecision.would_action != "allow"
                    )
                )
            ).all()
            guard_flags = [
                {
                    "stage": d.stage,
                    "policy": d.policy_id,
                    "mode": d.mode,
                    "action": d.action,
                    "would_action": d.would_action,
                    "tool": d.tool_name or "",
                }
                for d in flagged
            ]
    finally:
        await engine.dispose()

    attacked, hits = (
        attack_succeeded(scenario.attack_success, traj) if scenario.kind is Kind.ATTACK else (False, [])
    )
    done, failures = task_succeeded(scenario.task_success, traj)
    return ScenarioResult(
        scenario_id=scenario.id,
        split=scenario.split.value,
        kind=scenario.kind.value,
        config=config.name,
        status=traj.status,
        steps=traj.steps,
        cost_usd=cost,
        attack_success=attacked,
        attack_hits=hits,
        task_success=done,
        task_failures=failures,
        final_message=traj.final_message,
        run_id=response.run_id,
        tool_calls=[{"tool": c.tool_name, "arguments": c.arguments} for c in mcp.calls],
        guard_flags=guard_flags,
    )


async def _conversation_cost(session: AsyncSession, conversation_id: str) -> float:
    from boundary_agent.models import Conversation

    conversation = await session.get(Conversation, conversation_id)
    return float(conversation.spent_cost) if conversation else 0.0


async def run_matrix(
    scenarios: list[Scenario],
    resolved: dict[str, dict[str, Any]],
    configs: list[Config],
    *,
    policy_path: Path,
    planner_for: Any | None = None,
    settings_overrides: dict[str, Any] | None = None,
    enforce_timeouts: bool = False,
) -> E2ERun:
    run = E2ERun()
    for config in configs:
        for scenario in scenarios:
            planner = planner_for(scenario, config) if planner_for else None
            run.results.append(
                await run_scenario(
                    scenario,
                    resolved.get(scenario.id, {}),
                    config,
                    policy_path=policy_path,
                    planner=planner,
                    settings_overrides=settings_overrides,
                    enforce_timeouts=enforce_timeouts,
                )
            )
    return run
