"""App start and stop: database, MCP servers and their tools, default tool rules, the guard's stored mode
overrides and dashboard rules, a warm-up check per stage, and the sweeper that expires approvals."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boundary_agent import services
from boundary_agent.api.guard import apply_guard_overrides
from boundary_agent.api.guard_rules import load_guard_rules
from boundary_agent.db import get_session, init_db
from boundary_agent.models import AuditEvent, Policy
from boundary_guard import CheckContext, Stage

# Default taint rules: once a run has read content flagged as injection, mutating tools need a human.
DEFAULT_GUARD_SIGNAL_POLICIES = [
    {
        "name": "Tainted run: approve writes",
        "target_tool": "write_file",
        "reason": "This run read content flagged as possible prompt injection; writes need approval.",
    },
    {
        "name": "Tainted run: approve deletes",
        "target_tool": "delete_file",
        "reason": "This run read content flagged as possible prompt injection; deletes need approval.",
    },
]


async def seed_guard_signal_policies(session: AsyncSession) -> None:
    """Create the default taint policies on first start only. The audit log remembers that they were
    offered, so a default someone deleted or edited stays that way across restarts."""
    already = await session.scalar(
        select(AuditEvent.id).where(AuditEvent.event_type == "policy.defaults_seeded").limit(1)
    )
    if already is not None:
        return
    # Databases from before this marker: if any taint policy exists, the defaults were seeded then.
    # Mark the ones still named as shipped, so the dashboard can label them.
    existing = await session.scalar(select(Policy).where(Policy.rule_type == "guard_signal").limit(1))
    default_names = {item["name"] for item in DEFAULT_GUARD_SIGNAL_POLICIES}
    for old in (await session.scalars(select(Policy).where(Policy.name.in_(default_names)))).all():
        old.action_json = {**(old.action_json or {}), "default": True}
    if existing is None:
        for item in DEFAULT_GUARD_SIGNAL_POLICIES:
            session.add(
                Policy(
                    name=item["name"],
                    rule_type="guard_signal",
                    enabled=True,
                    mode="enforce",
                    priority=190,
                    target_tool=item["target_tool"],
                    conditions_json={"run_tainted": True},
                    action_json={"verdict": "require_approval", "reason": item["reason"], "default": True},
                )
            )
    await services.audit_logger.record(
        session,
        "policy.defaults_seeded",
        {"names": [item["name"] for item in DEFAULT_GUARD_SIGNAL_POLICIES], "created": existing is None},
    )


async def warm_up_guard() -> None:
    """One throwaway check per stage before serving: the first inference of each model is far slower
    than the rest (lazy init, and pages pulled back in on a memory-starved host), and with
    fail-closed timeouts that turned the first real request into a block. Marked source="warmup", so
    it is kept out of the database and the app's metrics panels."""
    for stage in Stage:
        with suppress(Exception):  # warm-up is best effort
            await services.guard.check(stage, "Warm-up request.", CheckContext(metadata={"source": "warmup"}))


async def approval_sweeper(stop_event: asyncio.Event) -> None:
    """Expire unanswered approvals in the background, so a run doesn't wait forever."""
    while not stop_event.is_set():
        async for session in get_session():
            expired = await services.agent_runtime.expire_pending_approvals(session)
            if expired:
                await session.commit()
            break
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=services.settings.approval_sweeper_interval_seconds
            )
        except TimeoutError:
            continue


async def _prepare(session: AsyncSession) -> None:
    settings = services.settings
    await services.mcp_manager.ensure_seed_servers(
        session,
        python_executable=sys.executable,
        exa_enabled=settings.exa_mcp_enabled,
        exa_url=settings.exa_mcp_url,
        exa_api_key=settings.exa_api_key,
        remote_url=settings.remote_mcp_url,
        remote_transport=settings.remote_mcp_transport,
        remote_name=settings.remote_mcp_name,
    )
    await services.mcp_manager.list_tools(session, refresh=True)
    if services.guard is not None:
        if settings.seed_guard_signal_policies:
            await seed_guard_signal_policies(session)
        await apply_guard_overrides(session)
        await load_guard_rules(session)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await services.broker.connect()
    # Shared budget and limits across replicas when Redis is on.
    services.spend.redis = services.limiter.redis = services.broker.redis
    sweeper_stop = asyncio.Event()
    sweeper_task = asyncio.create_task(approval_sweeper(sweeper_stop))
    async for session in get_session():
        await _prepare(session)
        await session.commit()
        break
    if services.guard is not None:
        await warm_up_guard()
    yield
    if services.guard is not None:
        await services.guard.drain()
    sweeper_stop.set()
    sweeper_task.cancel()
    with suppress(asyncio.CancelledError):
        await sweeper_task
    services.telemetry.flush()
    await services.broker.close()
