from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from redis import asyncio as redis_asyncio


class EventBroker:
    channel_name = "boundary.events"

    def __init__(self, redis_url: str | None) -> None:
        self.redis_url = redis_url
        self.redis = None
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    async def connect(self) -> None:
        if self.redis_url:
            self.redis = redis_asyncio.from_url(self.redis_url, decode_responses=True)

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()

    async def publish(self, event: dict[str, Any]) -> None:
        if self.redis is None:
            for subscriber in list(self.subscribers):
                await subscriber.put(event)
            return
        await self.redis.publish(self.channel_name, json.dumps(event, default=str))

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        if self.redis is None:
            self.subscribers.add(queue)
            try:
                yield queue
            finally:
                self.subscribers.discard(queue)
            return

        pubsub = self.redis.pubsub()
        await pubsub.subscribe(self.channel_name)
        pump_task = asyncio.create_task(self._pump_pubsub(pubsub, queue))
        try:
            yield queue
        finally:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
            await pubsub.unsubscribe(self.channel_name)
            await pubsub.aclose()

    async def _pump_pubsub(self, pubsub, queue: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not message:
                await asyncio.sleep(0.05)
                continue
            if message.get("type") != "message":
                continue
            payload = message.get("data")
            if isinstance(payload, str):
                await queue.put(json.loads(payload))
