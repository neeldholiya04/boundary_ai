"""Approvals: tool calls and content waiting for a person, and the decision that resumes the run."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boundary_agent import services
from boundary_agent.db import get_session
from boundary_agent.models import ApprovalRequest
from boundary_agent.schemas import ApprovalDecisionRequest

router = APIRouter(prefix="/api/approvals")


@router.get("")
async def list_approvals(session: AsyncSession = Depends(get_session)) -> list[dict]:
    await services.agent_runtime.expire_pending_approvals(session)
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


@router.post("/{approval_id}/decision")
async def decide_approval(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    session: AsyncSession = Depends(get_session),
):
    try:
        response = await services.agent_runtime.decide_approval(
            session, approval_id, payload.decision, payload.comment
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response.trace_url = services.telemetry.trace_url(response.run_id)
    return response


def _approval_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    if "__content__" in payload:
        call = payload.get("__tool_call__") or {}
        return call.get("arguments", {})
    return payload.get("__tool_arguments__", payload)
