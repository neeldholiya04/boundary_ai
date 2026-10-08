from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from boundary_agent.agent import AgentRuntime
from boundary_agent.audit import AuditLogger
from boundary_agent.auth import Authenticator, current_account, required_role, token_from
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
    GuardRule,
    MCPServer,
    Message,
    Policy,
    Run,
)
from boundary_agent.playground import build_router
from boundary_agent.policy import PolicyEngine, policy_mode
from boundary_agent.realtime import EventBroker
from boundary_agent.rules import RULE_PREFIX, RuleSpec, benign_records, compile_rule, dry_run, policy_id_for
from boundary_agent.schemas import (
    ApprovalDecisionRequest,
    ChatRequest,
    ConversationCreate,
    GuardModeRequest,
    GuardRuleTestRequest,
    LoginRequest,
    MCPServerCreate,
    MCPServerUpdate,
    PolicyCreate,
    PolicyUpdate,
    check_policy_shape,
)
from boundary_agent.secrets_mask import mask_config, unmask_config
from boundary_agent.telemetry import Telemetry
from boundary_guard import CheckContext, Guard, Mode, PolicyConfig, Stage
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
    await audit_logger.record(
        session,
        "policy.defaults_seeded",
        {"names": [item["name"] for item in DEFAULT_GUARD_SIGNAL_POLICIES], "created": existing is None},
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
            await _load_guard_rules(session)
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


authenticator = Authenticator(settings.auth_users, settings.auth_secret, settings.auth_token_hours)
# Failed logins per client: 10 per 5 minutes, then wait.
_login_failures: dict[str, list[float]] = {}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Every /api path needs the role auth.required_role gives it (default: admin). Added before CORS,
    so CORS wraps it and a 401 still carries the CORS headers the dashboard needs to read it."""
    role = required_role(request.url.path)
    if request.method == "OPTIONS" or role is None or not settings.auth_required:
        return await call_next(request)
    token = token_from(request)
    account = authenticator.verify(token) if token else None
    if account is None:
        return JSONResponse({"detail": "Sign in first."}, status_code=401)
    if role == "admin" and account.role != "admin":
        return JSONResponse({"detail": "This needs an admin account."}, status_code=403)
    request.state.account = account
    return await call_next(request)


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


@app.post("/api/auth/login")
async def login(payload: LoginRequest, request: Request) -> dict:
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    recent = [t for t in _login_failures.get(client, []) if now - t < 300]
    if len(recent) >= 10:
        raise HTTPException(status_code=429, detail="Too many failed sign-ins. Try again in a few minutes.")
    account = authenticator.login(payload.username.strip(), payload.password)
    if account is None:
        _login_failures[client] = [*recent, now]
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    _login_failures.pop(client, None)
    return {"token": authenticator.issue(account), "username": account.username, "role": account.role}


@app.get("/api/auth/me")
async def me(request: Request) -> dict:
    account = current_account(request)
    if account is None:  # auth switched off (tests, local tools)
        return {"username": "local", "role": "admin"}
    return {"username": account.username, "role": account.role}


def _owner_filter(request: Request):
    """Users see their own conversations; admins (and auth switched off) see all."""
    account = current_account(request)
    return None if account is None or account.role == "admin" else account.username


async def _owned_conversation(request: Request, session: AsyncSession, conversation_id: str) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    owner = _owner_filter(request)
    # Someone else's conversation is reported as missing, not forbidden: ids aren't confirmed.
    if conversation is None or (owner is not None and conversation.owner != owner):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@app.get("/api/conversations")
async def list_conversations(request: Request, session: AsyncSession = Depends(get_session)) -> list[dict]:
    await agent_runtime.expire_pending_approvals(session)
    await session.commit()
    query = select(Conversation).order_by(Conversation.created_at.desc())
    if (owner := _owner_filter(request)) is not None:
        query = query.where(Conversation.owner == owner)
    conversations = (await session.scalars(query)).all()
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
                # The run's user-facing line; the approval's own reason (guard scores) is for reviewers.
                "pending_approval_reason": (
                    (latest_run.paused_reason if latest_run else None) or "Waiting for a person's approval."
                )
                if pending_approval
                else None,
                "latest_message_preview": (latest_message.content[:120] if latest_message else ""),
            }
        )
    return summaries


@app.post("/api/conversations")
async def create_conversation(
    payload: ConversationCreate, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    account = current_account(request)
    conversation = Conversation(
        title=payload.title or "New conversation",
        owner=account.username if account else None,
        token_budget=payload.token_budget,
        cost_budget=payload.cost_budget,
    )
    session.add(conversation)
    await session.flush()
    await session.commit()
    return {"id": conversation.id}


@app.get("/api/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str, request: Request, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    await _owned_conversation(request, session, conversation_id)
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
async def chat(payload: ChatRequest, request: Request, session: AsyncSession = Depends(get_session)):
    account = current_account(request)
    if payload.conversation_id and await session.get(Conversation, payload.conversation_id) is not None:
        await _owned_conversation(request, session, payload.conversation_id)
    response = await agent_runtime.handle_chat(
        session, payload.message, payload.conversation_id, response_schema=payload.response_schema
    )
    conversation = await session.get(Conversation, response.conversation_id)
    if account is not None and conversation is not None and conversation.owner is None:
        conversation.owner = account.username
        await session.commit()
    # Traces are an operator tool (they show every guard decision); users don't get the link.
    if account is None or account.role == "admin":
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
            "mode": policy_mode(policy),
            "priority": policy.priority,
            "target_tool": policy.target_tool,
            "target_server_id": policy.target_server_id,
            "conditions": policy.conditions_json,
            "action": policy.action_json,
            "created_at": policy.created_at,
            "updated_at": policy.updated_at,
        }
        for policy in policies
    ]


def _policy_audit(policy: Policy) -> dict:
    """Everything that decides what a policy does, so the log shows each version."""
    return {
        "policy_id": policy.id,
        "name": policy.name,
        "rule_type": policy.rule_type,
        "mode": policy_mode(policy),
        "priority": policy.priority,
        "target_tool": policy.target_tool,
        "conditions": policy.conditions_json,
        "action": policy.action_json,
    }


@app.post("/api/policies")
async def create_policy(payload: PolicyCreate, session: AsyncSession = Depends(get_session)) -> dict:
    mode = payload.mode if payload.enabled else "off"
    policy = Policy(
        name=payload.name,
        rule_type=payload.rule_type,
        enabled=mode != "off",
        mode=mode,
        priority=payload.priority,
        target_tool=payload.target_tool,
        target_server_id=payload.target_server_id,
        conditions_json=payload.conditions,
        action_json=payload.action,
    )
    session.add(policy)
    await session.flush()
    await audit_logger.record(session, "policy.created", _policy_audit(policy))
    await session.commit()
    return {"id": policy.id}


@app.patch("/api/policies/{policy_id}")
async def update_policy(
    policy_id: str, payload: PolicyUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    policy = await session.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    changes = payload.model_dump(exclude_unset=True)
    # An explicit null only means something for the scope fields (a budget has no tool).
    changes = {k: v for k, v in changes.items() if v is not None or k in ("target_tool", "target_server_id")}
    # `mode` and the older `enabled` describe the same switch; keep them in step.
    if "mode" in changes:
        changes["enabled"] = changes["mode"] != "off"
    elif "enabled" in changes:
        current = policy_mode(policy)
        changes["mode"] = ("enforce" if current == "off" else current) if changes["enabled"] else "off"
    # Only a change to what the policy does is re-validated, so an old policy that fails today's checks
    # can still be switched off or to shadow.
    try:
        if {"rule_type", "conditions", "action"} & changes.keys():
            check_policy_shape(
                changes.get("rule_type", policy.rule_type),
                changes.get("conditions", policy.conditions_json),
                changes.get("action", policy.action_json),
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for field_name, value in changes.items():
        if field_name == "conditions":
            policy.conditions_json = value
        elif field_name == "action":
            policy.action_json = value
        else:
            setattr(policy, field_name, value)
    await audit_logger.record(session, "policy.updated", _policy_audit(policy))
    await session.commit()
    return {"ok": True}


@app.delete("/api/policies/{policy_id}")
async def delete_policy(policy_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    policy = await session.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    await audit_logger.record(session, "policy.deleted", {"policy_id": policy.id, "name": policy.name})
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
                # file: the reviewed baseline in the policy file; rule: written in the dashboard.
                "origin": "file" if guard.is_file_policy(p.id) else "rule",
                "description": p.description,
                "tools": p.tools,
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
        "rule_errors": rule_errors,
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
    if not guard.is_file_policy(policy_id) and not policy_id.startswith(RULE_PREFIX):
        raise HTTPException(status_code=404, detail=f"Unknown policy {policy_id}")

    if not guard.is_file_policy(policy_id):
        # A rule's mode is part of the rule (versioned and audited with it), not an override.
        rule = await session.scalar(select(GuardRule).where(GuardRule.policy_id == policy_id))
        if rule is None:
            raise HTTPException(status_code=404, detail=f"Unknown policy {policy_id}")
        return await _set_rule_mode(session, rule, payload.mode)

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


# ---- guard rules (written in the dashboard; see rules.py) ------------------------------------------

# Rules that failed to load at startup (e.g. a judge model that's gone), by policy id. They stay in the
# database and the dashboard so they can be fixed; the guard runs without them meanwhile.
rule_errors: dict[str, str] = {}
# One rule change at a time: each one updates the live guard and the database, and they must agree.
_rule_lock = asyncio.Lock()


def _judge_model() -> str:
    return settings.guard_judge_model or settings.llm_model


def _judge_models() -> set[str]:
    """Models a judge rule may name: the configured ones, so a rule can't send text to an arbitrary
    provider (or run up a bill on an expensive model)."""
    return {_judge_model(), settings.llm_model, *settings.guard_judge_models}


def _require_guard() -> Guard:
    if guard is None:
        raise HTTPException(
            status_code=409, detail="The guard is off (no GUARD_POLICY_PATH), so rules can't run"
        )
    return guard


def _rule_view(rule: GuardRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "policy_id": rule.policy_id,
        "version": rule.version,
        "spec": rule.spec_json,
        # In the guard: running (in any mode but off). A rule switched off, or one that failed to
        # load, is stored but not in the guard.
        "active": guard is not None and rule.policy_id in guard.policy_ids,
        "error": rule_errors.get(rule.policy_id),
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }


def _parse_spec(raw: dict[str, Any]) -> RuleSpec:
    try:
        spec = RuleSpec.model_validate(raw)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if spec.check.type == "llm_judge" and spec.check.model and spec.check.model not in _judge_models():
        allowed = ", ".join(sorted(_judge_models()))
        raise HTTPException(status_code=422, detail=f"judge model must be one of: {allowed}")
    return spec


def _compile(spec: RuleSpec, policy_id: str) -> PolicyConfig:
    try:
        return compile_rule(
            spec, policy_id, judge_model=_judge_model(), taint_labels=settings.guard_taint_labels
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _apply(live: Guard, policy_id: str, spec: RuleSpec) -> None:
    """Put `spec` into the live guard. A rule switched off is taken out instead, so a rule whose
    check can't currently be built (a removed judge model) can still be switched off."""
    if spec.mode == "off":
        if policy_id in live.policy_ids:
            live.remove_policy(policy_id)
        return
    try:
        live.add_policy(_compile(spec, policy_id), replace=policy_id in live.policy_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _restore(live: Guard, policy_id: str, previous: PolicyConfig | None) -> None:
    """Undo `_apply` after a failed commit, so the guard keeps matching the database."""
    with suppress(ValueError, KeyError):
        if previous is None:
            live.remove_policy(policy_id)
        else:
            live.add_policy(previous, replace=policy_id in live.policy_ids)


async def _save_rule(session: AsyncSession, rule: GuardRule, spec: RuleSpec, *, event: str) -> dict[str, Any]:
    """Apply `spec` to the live guard first (an invalid rule changes nothing), then persist and audit;
    if the commit fails, the guard goes back to what it was."""
    live = _require_guard()
    previous = next((p for p in live.config.policies if p.id == rule.policy_id), None)
    _apply(live, rule.policy_id, spec)
    rule_errors.pop(rule.policy_id, None)
    rule.spec_json = spec.model_dump(mode="json")
    if event != "guard.rule_created":
        rule.version += 1
    await audit_logger.record(
        session,
        event,
        {
            "rule_id": rule.id,
            "policy_id": rule.policy_id,
            "version": rule.version,
            "spec": rule.spec_json,
            "config_hash": live.config_hash,
        },
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        _restore(live, rule.policy_id, previous)
        raise
    telemetry.metadata["config_hash"] = live.config_hash
    return _rule_view(rule)


async def _set_rule_mode(session: AsyncSession, rule: GuardRule, mode: str) -> dict[str, Any]:
    async with _rule_lock:
        if rule.spec_json.get("mode") == mode and (
            mode == "off" or rule.policy_id in _require_guard().policy_ids
        ):
            return {"policy_id": rule.policy_id, "mode": mode, "config_hash": _require_guard().config_hash}
        spec = _parse_spec({**rule.spec_json, "mode": mode})
        await _save_rule(session, rule, spec, event="guard.rule_updated")
    return {"policy_id": rule.policy_id, "mode": mode, "config_hash": _require_guard().config_hash}


async def _load_guard_rules(session: AsyncSession) -> None:
    rule_errors.clear()
    for rule in (await session.scalars(select(GuardRule).order_by(GuardRule.created_at))).all():
        try:
            spec = RuleSpec.model_validate(rule.spec_json)
            if spec.mode != "off":
                guard.add_policy(
                    compile_rule(
                        spec,
                        rule.policy_id,
                        judge_model=_judge_model(),
                        taint_labels=settings.guard_taint_labels,
                    ),
                    replace=True,
                )
        except Exception as exc:  # one broken rule must not stop the app (or the other rules) loading
            rule_errors[rule.policy_id] = f"{type(exc).__name__}: {exc}"
            await audit_logger.record(
                session,
                "guard.rule_failed",
                {"rule_id": rule.id, "policy_id": rule.policy_id, "error": str(exc)},
            )
    telemetry.metadata["config_hash"] = guard.config_hash


async def _used_policy_ids(session: AsyncSession, live: Guard) -> set[str]:
    """Ids a new rule may not take: live policies, stored rules, and any id with decision history, so a
    recreated rule never inherits a deleted one's shadow/enforce statistics."""
    stored = set((await session.scalars(select(GuardRule.policy_id))).all())
    seen = set(
        (
            await session.scalars(
                select(GuardDecision.policy_id)
                .where(GuardDecision.policy_id.like(f"{RULE_PREFIX}%"))
                .distinct()
            )
        ).all()
    )
    return set(live.policy_ids) | stored | seen


@app.get("/api/guard/rules")
async def list_guard_rules(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rules = (await session.scalars(select(GuardRule).order_by(GuardRule.created_at))).all()
    return [_rule_view(rule) for rule in rules]


@app.post("/api/guard/rules", status_code=201)
async def create_guard_rule(payload: dict[str, Any], session: AsyncSession = Depends(get_session)) -> dict:
    live = _require_guard()
    spec = _parse_spec(payload)
    async with _rule_lock:
        rule = GuardRule(
            policy_id=policy_id_for(spec.name, await _used_policy_ids(session, live)), spec_json={}, version=1
        )
        session.add(rule)
        await session.flush()
        try:
            return await _save_rule(session, rule, spec, event="guard.rule_created")
        except HTTPException:
            await session.rollback()
            raise


@app.put("/api/guard/rules/{rule_id}")
async def replace_guard_rule(
    rule_id: str, payload: dict[str, Any], session: AsyncSession = Depends(get_session)
) -> dict:
    spec = _parse_spec(payload)
    async with _rule_lock:
        rule = await session.get(GuardRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        return await _save_rule(session, rule, spec, event="guard.rule_updated")


@app.patch("/api/guard/rules/{rule_id}")
async def set_guard_rule_mode(
    rule_id: str, payload: GuardModeRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    rule = await session.get(GuardRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    await _set_rule_mode(session, rule, payload.mode)
    return _rule_view(rule)


@app.delete("/api/guard/rules/{rule_id}")
async def delete_guard_rule(rule_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    live = _require_guard()
    async with _rule_lock:
        rule = await session.get(GuardRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        previous = next((p for p in live.config.policies if p.id == rule.policy_id), None)
        if previous is not None:
            live.remove_policy(rule.policy_id)
        await audit_logger.record(
            session,
            "guard.rule_deleted",
            {
                "rule_id": rule.id,
                "policy_id": rule.policy_id,
                "version": rule.version,
                "spec": rule.spec_json,
                "config_hash": live.config_hash,
            },
        )
        await session.delete(rule)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            _restore(live, rule.policy_id, previous)
            raise
        rule_errors.pop(rule.policy_id, None)
        telemetry.metadata["config_hash"] = live.config_hash
    return {"ok": True, "config_hash": live.config_hash}


@app.post("/api/guard/rules/test")
async def test_guard_rule(payload: GuardRuleTestRequest, request: Request) -> dict:
    """Dry-run a draft rule against its examples and benign eval records. Nothing is stored or applied."""
    spec = _parse_spec(payload.spec)
    _compile(spec, "rule_draft")
    judged = spec.check.type == "llm_judge"
    if judged:
        # Every example and sampled record is a model call: bound both, and how often it can be asked.
        cap = settings.guard_rule_test_examples_judge
        if len(spec.tests.should_fire) + len(spec.tests.should_pass) > cap:
            raise HTTPException(status_code=422, detail=f"judge rules are tested on at most {cap} examples")
        allowed, retry_after = await limiter.hit(
            "rule_test_judge", request.client.host if request.client else "?", limit=20, window_s=3600
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many judged dry runs; try again later.",
                headers={"Retry-After": str(retry_after)},
            )
    sample = settings.guard_rule_test_sample_judge if judged else settings.guard_rule_test_sample
    benign = benign_records(REPO_ROOT, spec.stages, limit=sample) if payload.sample_benign else None
    try:
        return await dry_run(
            spec,
            base_dir=guard_policy_path.parent if guard_policy_path else REPO_ROOT / "policies",
            judge_model=_judge_model(),
            taint_labels=settings.guard_taint_labels,
            benign_texts=benign,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/guard/rules/export", response_class=Response)
async def export_guard_rules(session: AsyncSession = Depends(get_session)) -> Response:
    """The running rules as a policy file fragment (same format as policies/guard.yaml), so a rule that
    has proved itself can be reviewed into the baseline, or measured with `boundary-eval detectors`."""
    import yaml

    live = _require_guard()
    rules = (await session.scalars(select(GuardRule).order_by(GuardRule.created_at))).all()
    running = {r.policy_id for r in rules} & set(live.policy_ids)
    policies = [p.model_dump(mode="json", exclude_none=True) for p in live.config.policies if p.id in running]
    left_out = sorted({r.policy_id for r in rules} - running)
    header = (
        f"# Guard rules exported from the dashboard. Config {live.config_hash}, "
        f"{datetime.now(UTC).isoformat(timespec='seconds')}.\n"
        "# Paths are relative to policies/; measure with:\n"
        "#   uv run boundary-eval detectors --suite golden --policies <this file>\n"
        + (f"# Not included (switched off or failed to load): {', '.join(left_out)}\n" if left_out else "")
    )
    body = yaml.safe_dump({"version": live.config.version, "policies": policies}, sort_keys=False)
    return Response(header + body, media_type="application/yaml")


@app.get("/api/guard/check-types")
async def guard_check_types() -> dict:
    """What a rule can check, for the dashboard's rule form."""
    return {
        "stages": [s.value for s in Stage],
        "tool_stages": [Stage.TOOL_ARGS.value, Stage.TOOL_OUTPUT.value],
        "judge_model": _judge_model(),
        "checks": [
            {
                "type": "keywords",
                "label": "Keywords",
                "can_redact": True,
                "hint": "Words or phrases, matched as whole words, any case.",
            },
            {
                "type": "pattern",
                "label": "Pattern",
                "can_redact": True,
                "hint": "Regular expressions; all of a rule's patterns share a 0.25 s budget per check.",
            },
            {
                "type": "topic",
                "label": "Topic",
                "can_redact": False,
                "hint": "Example requests about the topic; compared by meaning, not words.",
            },
            {
                "type": "llm_judge",
                "label": "Plain-language policy",
                "can_redact": False,
                "hint": (
                    "An LLM decides. Sends the checked text to the judge model's provider, costs a call "
                    "per check, and blocks if the judge errors: start in shadow."
                ),
            },
            {
                "type": "always",
                "label": "Every call",
                "can_redact": False,
                "hint": "For tool rules: every call to the chosen tools gets the action.",
            },
        ],
    }


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
