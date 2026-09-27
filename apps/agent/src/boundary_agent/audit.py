from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from boundary_agent.models import AuditEvent
from boundary_agent.realtime import EventBroker


class AuditLogger:
    def __init__(self, broker: EventBroker) -> None:
        self.broker = broker

    async def record(
        self,
        session: AsyncSession,
        event_type: str,
        payload: dict[str, Any],
        *,
        conversation_id: str | None = None,
        run_id: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            conversation_id=conversation_id,
            run_id=run_id,
            event_type=event_type,
            payload_json=payload,
        )
        session.add(event)
        await session.flush()
        await self.broker.publish({"type": event_type, "payload": payload})
        return event
