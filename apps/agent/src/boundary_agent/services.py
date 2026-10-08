"""The agent's runtime objects, built once at import: settings, the event broker, the audit log, the MCP
manager, the policy engine, telemetry, spend and rate limits, the guard (when a policy file is
configured), the agent runtime and the sign-in authenticator.

API modules read them as attributes of this module (`services.guard`), not as imported names, so
tests can swap one (e.g. a small file-defined guard) with `monkeypatch.setattr(services, "guard", …)`.
"""

from __future__ import annotations

from boundary_agent.agent import AgentRuntime
from boundary_agent.audit import AuditLogger
from boundary_agent.auth import Authenticator
from boundary_agent.config import get_settings
from boundary_agent.db import SessionLocal
from boundary_agent.guarding import GuardAdapter, GuardDecisionSink
from boundary_agent.limits import DailySpend, RateLimiter
from boundary_agent.llm import get_planner
from boundary_agent.mcp_manager import MCPManager
from boundary_agent.policy import PolicyEngine
from boundary_agent.realtime import EventBroker
from boundary_agent.telemetry import Telemetry
from boundary_guard import Guard
from boundary_guard.metrics import PrometheusSink

settings = get_settings()
broker = EventBroker(settings.redis_url)
audit_logger = AuditLogger(broker)
mcp_manager = MCPManager()
policy_engine = PolicyEngine()
telemetry = Telemetry.from_settings(settings)
spend = DailySpend(settings.llm_daily_budget_usd)  # Redis attached in lifespan when configured
limiter = RateLimiter()
authenticator = Authenticator(settings.auth_users, settings.auth_secret, settings.auth_token_hours)

guard_policy_path = settings.resolved_guard_policy_path()
guard = (
    Guard.from_yaml(guard_policy_path, sinks=[GuardDecisionSink(SessionLocal, broker), PrometheusSink()])
    if guard_policy_path
    else None
)
if guard is not None:
    telemetry.watch_guard_queue(guard)
    telemetry.metadata.update(policy_version=str(guard.config.version), config_hash=guard.config_hash)
guard_adapter = (
    GuardAdapter(guard, audit_logger, taint_labels=settings.guard_taint_labels, telemetry=telemetry)
    if guard
    else None
)
agent_runtime = AgentRuntime(
    settings,
    get_planner(settings, telemetry=telemetry, spend=spend),
    mcp_manager,
    policy_engine,
    audit_logger,
    guard=guard_adapter,
    telemetry=telemetry,
)
