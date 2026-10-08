"""The audit log and the live event stream the dashboard refreshes from (admin only)."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from boundary_agent import services
from boundary_agent.db import get_session
from boundary_agent.models import AuditEvent

router = APIRouter(prefix="/api")


@router.get("/logs")
async def list_logs(session: AsyncSession = Depends(get_session), limit: int = 100) -> list[dict]:
    events = (
        await session.scalars(
            select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(min(limit, 5000))
        )
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


@router.get("/events/stream")
async def stream_events(request: Request):
    async def event_generator():
        async with services.broker.subscribe() as queue:
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
