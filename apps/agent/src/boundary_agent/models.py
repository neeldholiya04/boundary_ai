from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from boundary_agent.db import Base


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    """Row timestamps are set here, at creation, with microsecond precision. The database's now() is
    the transaction start in Postgres (every row a request writes would tie) and whole seconds in
    SQLite, which made "the most recent N messages" an arbitrary pick among ties."""
    return datetime.now(UTC)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(255), default="New conversation")
    token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    spent_tokens: Mapped[int] = mapped_column(Integer, default=0)
    spent_cost: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), onupdate=_now
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    runs: Mapped[list[Run]] = relationship(back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(24))
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="running")
    latest_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Set once the run has read content the guard flagged as injection (even in shadow mode);
    # `guard_signal` policy rules can then require approval for mutating tools.
    tainted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    taint_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Schema id the caller asked the final answer to follow (e.g. "research_note.v1").
    response_schema: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), onupdate=_now
    )

    conversation: Mapped[Conversation] = relationship(back_populates="runs")
    approvals: Mapped[list[ApprovalRequest]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class MCPServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    transport: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config_json: Mapped[dict] = mapped_column(JSON)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), onupdate=_now
    )

    tools: Mapped[list[DiscoveredTool]] = relationship(back_populates="server", cascade="all, delete-orphan")


class DiscoveredTool(Base):
    __tablename__ = "discovered_tools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    discovered_at: Mapped[datetime] = _created_at()

    server: Mapped[MCPServer] = relationship(back_populates="tools")


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120))
    rule_type: Mapped[str] = mapped_column(String(40))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # off / shadow / enforce, like guard rules. Shadow logs what the policy would decide.
    mode: Mapped[str] = mapped_column(String(16), default="enforce", server_default="enforce")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    target_tool: Mapped[str | None] = mapped_column(String(120), nullable=True)
    target_server_id: Mapped[str | None] = mapped_column(ForeignKey("mcp_servers.id"), nullable=True)
    conditions_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    action_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), onupdate=_now
    )


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    # "tool_call" (policy engine / tool-args guard) or "content_review" (guard escalation on content).
    kind: Mapped[str] = mapped_column(String(24), default="tool_call", server_default="tool_call")
    # Guard stage for content reviews: user_input | tool_output | final_output.
    stage: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # Nullable because content reviews of user input or final answers involve no MCP server.
    server_id: Mapped[str | None] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), index=True, nullable=True
    )
    tool_name: Mapped[str] = mapped_column(String(120))
    arguments_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    reason: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[Run] = relationship(back_populates="approvals")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created_at()


class GuardDecision(Base):
    """One row per policy per guard check. Never stores the checked text: only a redacted excerpt
    (every matched span replaced) and its sha256."""

    __tablename__ = "guard_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True, nullable=True
    )
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    stage: Mapped[str] = mapped_column(String(24), index=True)
    tool_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    policy_id: Mapped[str] = mapped_column(String(80), index=True)
    policy_version: Mapped[str] = mapped_column(String(24))
    config_hash: Mapped[str] = mapped_column(String(32))
    detector: Mapped[str] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(16))
    execution: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(16))
    would_action: Mapped[str] = mapped_column(String(16), index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    reasons_json: Mapped[list] = mapped_column(JSON, default=list)
    span_labels_json: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    excerpt: Mapped[str] = mapped_column(Text, default="")
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()


class GuardOverride(Base):
    """Runtime mode override for a guard policy (set from the dashboard). Loaded at startup and
    applied to the in-memory Guard, so a shadow/enforce/off change survives a restart."""

    __tablename__ = "guard_overrides"

    policy_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), onupdate=_now
    )


class GuardRule(Base):
    """A guard rule written in the dashboard (see rules.py). `spec_json` is the rule as the operator
    wrote it; it compiles into the guard policy `policy_id`, loaded at startup and on every change.
    Every change bumps `version` and is audited with the full spec, which is the rule's history."""

    __tablename__ = "guard_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    policy_id: Mapped[str] = mapped_column(String(80), unique=True)
    spec_json: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), onupdate=_now
    )
