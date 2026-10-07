from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from boundary_agent.agent import AgentRuntime
from boundary_agent.audit import AuditLogger
from boundary_agent.config import REPO_ROOT, get_settings
from boundary_agent.db import SessionLocal, get_session, init_db
from boundary_agent.guarding import (
    GuardAdapter,
    GuardDecisionSink,
    aggregate_stats,
    apply_overrides,
    mode_counts,
)
from boundary_agent.limits import DailySpend, RateLimiter
from boundary_agent.llm import get_planner
from boundary_agent.mcp_manager import MCPManager
from boundary_agent.models import (
    ApprovalRequest,
    AuditEvent,
    Conversation,
    GuardDecision,
    GuardOverride,
    MCPServer,
    Message,
    Policy,
    Run,
)
from boundary_agent.playground import build_router
from boundary_agent.policy import PolicyEngine
from boundary_agent.realtime import EventBroker
from boundary_agent.schemas import (
    ApprovalDecisionRequest,
    ChatRequest,
    ConversationCreate,
    GuardModeRequest,
    MCPServerCreate,
    MCPServerUpdate,
    PolicyCreate,
    PolicyUpdate,
)
from boundary_agent.secrets_mask import mask_config, unmask_config
from boundary_agent.telemetry import Telemetry
from boundary_guard import CheckContext, Guard, Mode, Stage
from boundary_guard.metrics import PrometheusSink

settings = get_settings()
broker = EventBroker(settings.redis_url)
audit_logger = AuditLogger(broker)
mcp_manager = MCPManager()
policy_engine = PolicyEngine()
telemetry = Telemetry.from_settings(settings)
spend = DailySpend(settings.llm_daily_budget_usd)  # Redis attached in lifespan when configured
limiter = RateLimiter()
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


async def _seed_guard_signal_policies(session: AsyncSession) -> None:
    existing = await session.scalar(select(Policy).where(Policy.rule_type == "guard_signal").limit(1))
    if existing is not None:
        return
    for item in DEFAULT_GUARD_SIGNAL_POLICIES:
        session.add(
            Policy(
                name=item["name"],
                rule_type="guard_signal",
                enabled=True,
                priority=190,
                target_tool=item["target_tool"],
                conditions_json={"run_tainted": True},
                action_json={"verdict": "require_approval", "reason": item["reason"]},
            )
        )


async def _warm_up_guard() -> None:
    """One throwaway check per stage before serving: the first inference of each model is far slower
    than the rest (lazy init, and pages pulled back in on a memory-starved host), and with
    fail-closed timeouts that turned the first real request into a block. Marked source="warmup", so
    it is kept out of the database and the app's metrics panels."""
    for stage in Stage:
        with suppress(Exception):  # warm-up is best effort
            await guard.check(stage, "Warm-up request.", CheckContext(metadata={"source": "warmup"}))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await broker.connect()
    spend.redis = limiter.redis = broker.redis  # shared budget/limits across replicas when Redis is on
    sweeper_stop = asyncio.Event()
    sweeper_task = asyncio.create_task(_approval_sweeper(sweeper_stop))
    async for session in get_session():
        await mcp_manager.ensure_seed_servers(
            session,
            python_executable=sys.executable,
            exa_enabled=settings.exa_mcp_enabled,
            exa_url=settings.exa_mcp_url,
            exa_api_key=settings.exa_api_key,
            remote_url=settings.remote_mcp_url,
            remote_transport=settings.remote_mcp_transport,
            remote_name=settings.remote_mcp_name,
        )
        await mcp_manager.list_tools(session, refresh=True)
        if guard is not None and settings.seed_guard_signal_policies:
            await _seed_guard_signal_policies(session)
        if guard is not None:
            await _apply_guard_overrides(session)
        await session.commit()
        break
    if guard is not None:
        await _warm_up_guard()
    yield
    if guard is not None:
        await guard.drain()
    sweeper_stop.set()
    sweeper_task.cancel()
    with suppress(asyncio.CancelledError):
        await sweeper_task
    telemetry.flush()
    await broker.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


def _origins(origin: str) -> list[str]:
    """The configured dashboard origin plus its localhost/127.0.0.1 twin (browsers treat them apart)."""
    twins = {
        origin,
        origin.replace("://localhost", "://127.0.0.1"),
        origin.replace("://127.0.0.1", "://localhost"),
    }
    return sorted(twins)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins(settings.frontend_origin),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(
    build_router(
        settings=settings, guard=guard, telemetry=telemetry, spend=spend, limiter=limiter, repo_root=REPO_ROOT
    )
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape endpoint. Keep it off the public internet (Phase 12: proxy-restrict it)."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/conversations")
async def list_conversations(session: AsyncSession = Depends(get_session)) -> list[dict]:
    await agent_runtime.expire_pending_approvals(session)
    await session.commit()
    conversations = (
        await session.scalars(select(Conversation).order_by(Conversation.created_at.desc()))
    ).all()
    summaries = []
    for conversation in conversations:
        latest_run = await session.scalar(
            select(Run).where(Run.conversation_id == conversation.id).order_by(Run.created_at.desc()).limit(1)
        )
        pending_approval = await session.scalar(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.conversation_id == conversation.id,
                ApprovalRequest.status == "pending",
            )
            .order_by(ApprovalRequest.created_at.desc())
            .limit(1)
        )
        latest_message = await session.scalar(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        summaries.append(
            {
                "id": conversation.id,
                "title": conversation.title,
                "token_budget": conversation.token_budget,
                "cost_budget": conversation.cost_budget,
                "spent_tokens": conversation.spent_tokens,
                "spent_cost": conversation.spent_cost,
                "created_at": conversation.created_at,
                "updated_at": conversation.updated_at,
                "latest_run_status": latest_run.status if latest_run else "idle",
                "pending_approval": pending_approval is not None,
                "pending_approval_reason": pending_approval.reason if pending_approval else None,
                "latest_message_preview": (latest_message.content[:120] if latest_message else ""),
            }
        )
    return summaries


@app.post("/api/conversations")
async def create_conversation(
    payload: ConversationCreate, session: AsyncSession = Depends(get_session)
) -> dict:
    conversation = Conversation(
        title=payload.title or "New conversation",
        token_budget=payload.token_budget,
        cost_budget=payload.cost_budget,
    )
    session.add(conversation)
    await session.flush()
    await session.commit()
    return {"id": conversation.id}


@app.get("/api/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    messages = (
        await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
        )
    ).all()
    return [
        {
            "id": message.id,
            "role": message.role,
            "content": message.content,
            "metadata": message.metadata_json,
            "created_at": message.created_at,
        }
        for message in messages
    ]


@app.post("/api/chat")
async def chat(payload: ChatRequest, session: AsyncSession = Depends(get_session)):
    response = await agent_runtime.handle_chat(
        session, payload.message, payload.conversation_id, response_schema=payload.response_schema
    )
    response.trace_url = telemetry.trace_url(response.run_id)
    return response


@app.get("/api/runs/{run_id}/trace")
async def run_trace(run_id: str) -> dict:
    return {"enabled": telemetry.enabled, "url": telemetry.trace_url(run_id)}


@app.get("/api/mcp/servers")
async def list_mcp_servers(session: AsyncSession = Depends(get_session)) -> list[dict]:
    servers = (
        await session.scalars(
            select(MCPServer).options(selectinload(MCPServer.tools)).order_by(MCPServer.name.asc())
        )
    ).all()
    return [
        {
            "id": server.id,
            "name": server.name,
            "transport": server.transport,
            "enabled": server.enabled,
            "config": mask_config(server.config_json),
            "last_error": server.last_error,
            "last_discovered_at": server.last_discovered_at,
            "tool_count": len(server.tools),
            "status": _server_status(server),
        }
        for server in servers
    ]


@app.post("/api/mcp/servers")
async def create_mcp_server(payload: MCPServerCreate, session: AsyncSession = Depends(get_session)) -> dict:
    server = MCPServer(
        name=payload.name,
        transport=payload.transport,
        enabled=payload.enabled,
        config_json=payload.config,
    )
    session.add(server)
    await session.flush()
    await audit_logger.record(session, "mcp.server_created", {"server_id": server.id, "name": server.name})
    await session.commit()
    return {"id": server.id}


@app.patch("/api/mcp/servers/{server_id}")
async def update_mcp_server(
    server_id: str, payload: MCPServerUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    server = await session.get(MCPServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if payload.enabled is not None:
        server.enabled = payload.enabled
    if payload.config is not None:
        server.config_json = unmask_config(payload.config, server.config_json)
    await audit_logger.record(
        session,
        "mcp.server_updated",
        {"server_id": server.id, "enabled": server.enabled},
    )
    await session.commit()
    return {"ok": True}


@app.post("/api/mcp/servers/{server_id}/refresh")
async def refresh_mcp_server(server_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    server = await session.get(MCPServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    try:
        tools = await mcp_manager.refresh_server_tools(session, server)
        await audit_logger.record(
            session,
            "mcp.server_refreshed",
            {"server_id": server.id, "tool_count": len(tools)},
        )
        await session.commit()
        return {"tool_count": len(tools)}
    except Exception as exc:
        server.last_error = str(exc)
        await session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/mcp/tools")
async def list_mcp_tools(session: AsyncSession = Depends(get_session)) -> list[dict]:
    tools = await mcp_manager.list_tools(session, refresh=False)
    return [
        {
            "server_id": tool.server_id,
            "server_name": tool.server_name,
            "transport": tool.transport,
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in tools
    ]


@app.get("/api/policies")
async def list_policies(session: AsyncSession = Depends(get_session)) -> list[dict]:
    policies = (
        await session.scalars(select(Policy).order_by(Policy.priority.desc(), Policy.created_at.desc()))
    ).all()
    return [
        {
            "id": policy.id,
            "name": policy.name,
            "rule_type": policy.rule_type,
            "enabled": policy.enabled,
            "priority": policy.priority,
            "target_tool": policy.target_tool,
            "target_server_id": policy.target_server_id,
            "conditions": policy.conditions_json,
            "action": policy.action_json,
            "created_at": policy.created_at,
        }
        for policy in policies
    ]


@app.post("/api/policies")
async def create_policy(payload: PolicyCreate, session: AsyncSession = Depends(get_session)) -> dict:
    policy = Policy(
        name=payload.name,
        rule_type=payload.rule_type,
        enabled=payload.enabled,
        priority=payload.priority,
        target_tool=payload.target_tool,
        target_server_id=payload.target_server_id,
        conditions_json=payload.conditions,
        action_json=payload.action,
    )
    session.add(policy)
    await session.flush()
    await audit_logger.record(
        session,
        "policy.created",
        {"policy_id": policy.id, "name": policy.name, "rule_type": policy.rule_type},
    )
    await session.commit()
    return {"id": policy.id}


@app.patch("/api/policies/{policy_id}")
async def update_policy(
    policy_id: str, payload: PolicyUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    policy = await session.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        if field_name == "conditions":
            policy.conditions_json = value
        elif field_name == "action":
            policy.action_json = value
        else:
            setattr(policy, field_name, value)
    await audit_logger.record(
        session,
        "policy.updated",
        {"policy_id": policy.id, "enabled": policy.enabled, "priority": policy.priority},
    )
    await session.commit()
    return {"ok": True}


@app.delete("/api/policies/{policy_id}")
async def delete_policy(policy_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    policy = await session.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    await audit_logger.record(session, "policy.deleted", {"policy_id": policy.id})
    await session.delete(policy)
    await session.commit()
    return {"ok": True}


@app.get("/api/approvals")
async def list_approvals(session: AsyncSession = Depends(get_session)) -> list[dict]:
    await agent_runtime.expire_pending_approvals(session)
    await session.commit()
    approvals = (
        await session.scalars(select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc()))
    ).all()
    return [
        {
            "id": approval.id,
            "run_id": approval.run_id,
            "conversation_id": approval.conversation_id,
            "server_id": approval.server_id,
            "tool_name": approval.tool_name,
            "kind": approval.kind,
            "stage": approval.stage,
            "arguments": _approval_arguments(approval.arguments_json),
            "content": (approval.arguments_json or {}).get("__content__")
            if approval.kind == "content_review"
            else None,
            "status": approval.status,
            "reason": approval.reason,
            "expires_at": approval.expires_at,
            "comment": approval.decision_comment,
            "created_at": approval.created_at,
        }
        for approval in approvals
    ]


@app.post("/api/approvals/{approval_id}/decision")
async def decide_approval(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    session: AsyncSession = Depends(get_session),
):
    try:
        response = await agent_runtime.decide_approval(
            session, approval_id, payload.decision, payload.comment
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response.trace_url = telemetry.trace_url(response.run_id)
    return response


@app.get("/api/guard/status")
async def guard_status() -> dict:
    if guard is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "policy_file": str(guard_policy_path),
        "version": guard.config.version,
        "config_hash": guard.config_hash,
        "modes": mode_counts(guard),
        "policies": [
            {
                "id": p.id,
                "stages": [stage.value for stage in p.stages],
                "detector": p.detector.type,
                "action": p.action.value,
                "mode": guard.mode_of(p.id).value,
                "execution": p.execution.value,
                "detects": p.detects,
                "threshold": guard.threshold_of(p.id),
            }
            for p in guard.config.policies
        ],
        "dropped_async": guard.dropped_async,
        "pending_async": guard.pending_async,
        "tracing": telemetry.enabled,
    }


async def _apply_guard_overrides(session: AsyncSession) -> None:
    if guard is None:
        return
    rows = (await session.scalars(select(GuardOverride))).all()
    apply_overrides(guard, [(o.policy_id, o.mode) for o in rows])
    telemetry.metadata["config_hash"] = guard.config_hash


@app.patch("/api/guard/policies/{policy_id}")
async def set_guard_mode(
    policy_id: str, payload: GuardModeRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    if guard is None:
        raise HTTPException(status_code=404, detail="Guard is not enabled")
    if policy_id not in guard.policy_ids:
        raise HTTPException(status_code=404, detail=f"Unknown policy {policy_id}")

    previous = guard.mode_of(policy_id).value
    guard.set_mode(policy_id, Mode(payload.mode))
    telemetry.metadata["config_hash"] = guard.config_hash  # traces carry the hash in force
    # Persist as an override, or delete it when the mode matches the YAML default.
    default = next(p for p in guard.config.policies if p.id == policy_id).mode.value
    existing = await session.get(GuardOverride, policy_id)
    if payload.mode == default:
        if existing is not None:
            await session.delete(existing)
    elif existing is not None:
        existing.mode = payload.mode
    else:
        session.add(GuardOverride(policy_id=policy_id, mode=payload.mode))
    await audit_logger.record(
        session,
        "guard.mode_changed",
        {"policy_id": policy_id, "from": previous, "to": payload.mode, "config_hash": guard.config_hash},
    )
    await session.commit()
    return {"policy_id": policy_id, "mode": payload.mode, "config_hash": guard.config_hash}


@app.get("/api/guard/stats")
async def guard_stats(session: AsyncSession = Depends(get_session), limit: int = 5000) -> dict:
    """Per-policy would-action breakdown from recent decisions, for the shadow-vs-enforce view."""
    if guard is None:
        return {"enabled": False}
    rows = (
        await session.scalars(select(GuardDecision).order_by(GuardDecision.created_at.desc()).limit(limit))
    ).all()
    return {
        "enabled": True,
        "sampled": len(rows),
        "config_hash": guard.config_hash,
        "policies": aggregate_stats(guard, rows),
    }


@app.get("/api/guard/decisions")
async def list_guard_decisions(
    session: AsyncSession = Depends(get_session),
    run_id: str | None = None,
    policy_id: str | None = None,
    fired_only: bool = False,
    limit: int = 200,
) -> list[dict]:
    query = select(GuardDecision).order_by(GuardDecision.created_at.desc()).limit(min(limit, 1000))
    if run_id:
        query = query.where(GuardDecision.run_id == run_id)
    if policy_id:
        query = query.where(GuardDecision.policy_id == policy_id)
    if fired_only:
        query = query.where(GuardDecision.would_action != "allow")
    rows = (await session.scalars(query)).all()
    return [
        {
            "id": row.id,
            "run_id": row.run_id,
            "conversation_id": row.conversation_id,
            "stage": row.stage,
            "tool_name": row.tool_name,
            "policy_id": row.policy_id,
            "mode": row.mode,
            "execution": row.execution,
            "action": row.action,
            "would_action": row.would_action,
            "score": row.score,
            "threshold": row.threshold,
            "latency_ms": row.latency_ms,
            "reasons": row.reasons_json,
            "span_labels": row.span_labels_json,
            "error": row.error,
            "excerpt": row.excerpt,
            "config_hash": row.config_hash,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@app.get("/api/logs")
async def list_logs(session: AsyncSession = Depends(get_session), limit: int = 100) -> list[dict]:
    events = (
        await session.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit))
    ).all()
    return [
        {
            "id": event.id,
            "conversation_id": event.conversation_id,
            "run_id": event.run_id,
            "event_type": event.event_type,
            "payload": event.payload_json,
            "created_at": event.created_at,
        }
        for event in events
    ]


@app.get("/api/events/stream")
async def stream_events(request: Request):
    async def event_generator():
        async with broker.subscribe() as queue:
            yield {"event": "ready", "data": json.dumps({"message": "connected"})}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield {"event": event["type"], "data": json.dumps(event, default=str)}
                except TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"ts": "keepalive"})}

    return EventSourceResponse(event_generator())


def _server_status(server: MCPServer) -> str:
    if not server.enabled:
        return "disabled"
    if server.last_error:
        lowered = server.last_error.lower()
        if "401" in lowered or "403" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
            return "auth_error"
        if server.last_discovered_at:
            return "execution_failed"
        return "discovery_failed"
    if server.last_discovered_at and server.tools:
        return "connected"
    return "pending"


def _approval_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    if "__content__" in payload:
        call = payload.get("__tool_call__") or {}
        return call.get("arguments", {})
    return payload.get("__tool_arguments__", payload)


async def _approval_sweeper(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        async for session in get_session():
            expired = await agent_runtime.expire_pending_approvals(session)
            if expired:
                await session.commit()
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.approval_sweeper_interval_seconds)
        except TimeoutError:
            continue
