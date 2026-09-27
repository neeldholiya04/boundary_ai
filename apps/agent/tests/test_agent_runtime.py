from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boundary_agent.agent import AgentRuntime
from boundary_agent.audit import AuditLogger
from boundary_agent.config import Settings
from boundary_agent.db import Base
from boundary_agent.models import ApprovalRequest, AuditEvent, Conversation, MCPServer, Policy, Run
from boundary_agent.policy import PolicyEngine
from boundary_agent.realtime import EventBroker
from boundary_agent.types import PlannerDecision, ToolCall, ToolDescriptor


@dataclass
class StubPlanner:
    decisions: list[PlannerDecision]
    calls: int = 0

    async def plan(self, user_message, tools, executed_steps, conversation_history) -> PlannerDecision:
        decision = self.decisions[min(self.calls, len(self.decisions) - 1)]
        self.calls += 1
        return decision


class StubMCPManager:
    def __init__(self, tools: list[ToolDescriptor], tool_result: dict | None = None) -> None:
        self.tools = tools
        self.tool_result = tool_result or {"ok": True}
        self.calls: list[tuple[str, str, dict]] = []

    async def list_tools(self, session: AsyncSession, refresh: bool = False) -> list[ToolDescriptor]:
        return self.tools

    async def call_tool(self, session: AsyncSession, server_id: str, tool_name: str, arguments: dict) -> dict:
        self.calls.append((server_id, tool_name, arguments))
        return self.tool_result


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as db_session:
        yield db_session

    await engine.dispose()


async def seed_server(session: AsyncSession) -> MCPServer:
    server = MCPServer(name="local-sandbox", transport="stdio", enabled=True, config_json={})
    session.add(server)
    await session.flush()
    return server


def make_runtime(planner: StubPlanner, mcp_manager: StubMCPManager) -> AgentRuntime:
    settings = Settings(
        llm_provider="mock",
        allow_demo_mock_planner=True,
        approval_ttl_seconds=60,
        max_tool_steps=4,
    )
    return AgentRuntime(settings, planner, mcp_manager, PolicyEngine(), AuditLogger(EventBroker(None)))


@pytest.mark.anyio
async def test_blocked_tool_never_reaches_mcp(session: AsyncSession) -> None:
    server = await seed_server(session)
    session.add(
        Policy(
            name="Block deletes",
            rule_type="block_tool",
            enabled=True,
            priority=200,
            target_tool="delete_file",
            target_server_id=server.id,
            conditions_json={},
            action_json={"reason": "No deletes"},
        )
    )
    await session.commit()

    planner = StubPlanner(
        [
            PlannerDecision(
                assistant_message=None,
                tool_call=ToolCall(server.id, "delete_file", {"path": "notes/demo.txt"}),
            )
        ]
    )
    manager = StubMCPManager(
        [ToolDescriptor(server.id, server.name, server.transport, "delete_file", None, {"type": "object"})]
    )
    runtime = make_runtime(planner, manager)

    response = await runtime.handle_chat(session, "delete the file notes/demo.txt", None)

    assert response.status == "blocked"
    assert manager.calls == []


@pytest.mark.anyio
async def test_approval_resume_executes_when_policy_still_requires_approval(session: AsyncSession) -> None:
    server = await seed_server(session)
    session.add(
        Policy(
            name="Approve writes",
            rule_type="require_approval",
            enabled=True,
            priority=150,
            target_tool="write_file",
            target_server_id=server.id,
            conditions_json={},
            action_json={"reason": "Needs review"},
        )
    )
    await session.commit()

    planner = StubPlanner(
        [
            PlannerDecision(
                assistant_message=None,
                tool_call=ToolCall(server.id, "write_file", {"path": "notes/demo.txt", "content": "hello"}),
            ),
            PlannerDecision(assistant_message="Write completed."),
        ]
    )
    manager = StubMCPManager(
        [ToolDescriptor(server.id, server.name, server.transport, "write_file", None, {"type": "object"})],
        tool_result={"path": "notes/demo.txt", "bytes_written": 5},
    )
    runtime = make_runtime(planner, manager)

    waiting = await runtime.handle_chat(session, "write file notes/demo.txt: hello", None)
    approval = await session.get(ApprovalRequest, waiting.approval_request_id)

    assert waiting.status == "waiting_approval"
    assert approval is not None

    resumed = await runtime.decide_approval(session, approval.id, "approved", None)

    assert resumed.status == "completed"
    assert manager.calls == [(server.id, "write_file", {"path": "notes/demo.txt", "content": "hello"})]


@pytest.mark.anyio
async def test_approval_resume_blocks_when_policy_changes(session: AsyncSession) -> None:
    server = await seed_server(session)
    session.add(
        Policy(
            name="Approve writes",
            rule_type="require_approval",
            enabled=True,
            priority=150,
            target_tool="write_file",
            target_server_id=server.id,
            conditions_json={},
            action_json={"reason": "Needs review"},
        )
    )
    await session.commit()

    planner = StubPlanner(
        [
            PlannerDecision(
                assistant_message=None,
                tool_call=ToolCall(server.id, "write_file", {"path": "notes/demo.txt", "content": "hello"}),
            )
        ]
    )
    manager = StubMCPManager(
        [ToolDescriptor(server.id, server.name, server.transport, "write_file", None, {"type": "object"})]
    )
    runtime = make_runtime(planner, manager)

    waiting = await runtime.handle_chat(session, "write file notes/demo.txt: hello", None)
    approval = await session.get(ApprovalRequest, waiting.approval_request_id)
    session.add(
        Policy(
            name="Emergency block",
            rule_type="block_tool",
            enabled=True,
            priority=500,
            target_tool="write_file",
            target_server_id=server.id,
            conditions_json={},
            action_json={"reason": "Write access revoked"},
        )
    )
    await session.commit()

    resumed = await runtime.decide_approval(session, approval.id, "approved", None)
    refreshed = await session.get(ApprovalRequest, approval.id)
    events = (await session.scalars(select(AuditEvent).order_by(AuditEvent.created_at.asc()))).all()

    assert resumed.status == "blocked"
    assert "policy changed" in resumed.assistant_message.lower()
    assert manager.calls == []
    assert refreshed.status == "superseded"
    assert any(event.event_type == "approval.invalidated" for event in events)


@pytest.mark.anyio
async def test_pending_approval_auto_expires(session: AsyncSession) -> None:
    conversation = Conversation(title="Approval test")
    session.add(conversation)
    await session.flush()
    run = Run(conversation_id=conversation.id, status="waiting_approval")
    session.add(run)
    await session.flush()
    approval = ApprovalRequest(
        run_id=run.id,
        conversation_id=conversation.id,
        server_id="server-1",
        tool_name="write_file",
        arguments_json={"__tool_arguments__": {"path": "notes/demo.txt"}},
        status="pending",
        reason="Needs review",
        expires_at=datetime.utcnow() - timedelta(seconds=5),
    )
    session.add(approval)
    await session.commit()

    runtime = make_runtime(StubPlanner([PlannerDecision(assistant_message="unused")]), StubMCPManager([]))

    expired = await runtime.expire_pending_approvals(session)
    await session.commit()
    refreshed = await session.get(ApprovalRequest, approval.id)
    refreshed_run = await session.get(Run, run.id)

    assert expired == 1
    assert refreshed.status == "expired"
    assert refreshed_run.status == "denied"


@pytest.mark.anyio
async def test_token_budget_blocks_before_extra_tool_step(session: AsyncSession) -> None:
    server = await seed_server(session)
    conversation = Conversation(title="Budgeted", token_budget=5, spent_tokens=0)
    session.add(conversation)
    await session.commit()

    planner = StubPlanner(
        [
            PlannerDecision(
                assistant_message=None,
                tool_call=ToolCall(server.id, "list_files", {"path": "."}),
                usage_tokens=5,
            )
        ]
    )
    manager = StubMCPManager(
        [ToolDescriptor(server.id, server.name, server.transport, "list_files", None, {"type": "object"})]
    )
    runtime = make_runtime(planner, manager)

    response = await runtime.handle_chat(session, "list files", conversation.id)

    assert response.status == "blocked"
    assert "token budget exceeded" in response.assistant_message.lower()
    assert manager.calls == []


@pytest.mark.anyio
async def test_safe_tool_flow_records_audit_events(session: AsyncSession) -> None:
    server = await seed_server(session)
    planner = StubPlanner(
        [
            PlannerDecision(
                assistant_message=None, tool_call=ToolCall(server.id, "list_files", {"path": "."})
            ),
            PlannerDecision(assistant_message="Listed files safely."),
        ]
    )
    manager = StubMCPManager(
        [ToolDescriptor(server.id, server.name, server.transport, "list_files", None, {"type": "object"})],
        tool_result={"entries": []},
    )
    runtime = make_runtime(planner, manager)

    response = await runtime.handle_chat(session, "list the files", None)
    events = (await session.scalars(select(AuditEvent).order_by(AuditEvent.created_at.asc()))).all()

    assert response.status == "completed"
    assert manager.calls == [(server.id, "list_files", {"path": "."})]
    assert any(event.event_type == "policy.decision" for event in events)
    assert any(event.event_type == "mcp.tool_succeeded" for event in events)
