"""The chat: conversations (each user sees their own), their messages, sending a message, run traces."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boundary_agent import services
from boundary_agent.auth import current_account
from boundary_agent.db import get_session
from boundary_agent.models import ApprovalRequest, Conversation, Message, Run
from boundary_agent.schemas import ChatRequest, ConversationCreate

router = APIRouter()


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


@router.get("/api/conversations")
async def list_conversations(request: Request, session: AsyncSession = Depends(get_session)) -> list[dict]:
    await services.agent_runtime.expire_pending_approvals(session)
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


@router.post("/api/conversations")
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


@router.get("/api/conversations/{conversation_id}/messages")
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


@router.post("/api/chat")
async def chat(payload: ChatRequest, request: Request, session: AsyncSession = Depends(get_session)):
    account = current_account(request)
    if payload.conversation_id and await session.get(Conversation, payload.conversation_id) is not None:
        await _owned_conversation(request, session, payload.conversation_id)
    response = await services.agent_runtime.handle_chat(
        session, payload.message, payload.conversation_id, response_schema=payload.response_schema
    )
    conversation = await session.get(Conversation, response.conversation_id)
    if account is not None and conversation is not None and conversation.owner is None:
        conversation.owner = account.username
        await session.commit()
    # Traces are an operator tool (they show every guard decision); users don't get the link.
    if account is None or account.role == "admin":
        response.trace_url = services.telemetry.trace_url(response.run_id)
    return response


@router.get("/api/runs/{run_id}/trace")
async def run_trace(run_id: str) -> dict:
    return {"enabled": services.telemetry.enabled, "url": services.telemetry.trace_url(run_id)}
