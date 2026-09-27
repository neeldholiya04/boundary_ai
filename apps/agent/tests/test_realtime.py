from __future__ import annotations

import asyncio

import pytest

from boundary_agent.realtime import EventBroker


class FakePubSub:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.channel: str | None = None
        self.queue: asyncio.Queue[dict] = asyncio.Queue()

    async def subscribe(self, channel: str) -> None:
        self.channel = channel
        self.redis.pubsubs.append(self)

    async def unsubscribe(self, channel: str) -> None:
        if self in self.redis.pubsubs:
            self.redis.pubsubs.remove(self)

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 1.0):
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def aclose(self) -> None:
        return None


class FakeRedis:
    def __init__(self) -> None:
        self.pubsubs: list[FakePubSub] = []

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self)

    async def publish(self, channel: str, payload: str) -> None:
        for pubsub in list(self.pubsubs):
            if pubsub.channel == channel:
                await pubsub.queue.put({"type": "message", "data": payload})


@pytest.mark.anyio
async def test_event_broker_uses_redis_pubsub_when_available() -> None:
    broker = EventBroker("redis://fake")
    broker.redis = FakeRedis()

    async with broker.subscribe() as queue:
        await broker.publish({"type": "policy.updated", "payload": {"id": "policy-1"}})
        event = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert event == {"type": "policy.updated", "payload": {"id": "policy-1"}}
