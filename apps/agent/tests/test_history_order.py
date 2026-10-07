from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boundary_agent.agent import AgentRuntime
from boundary_agent.db import Base
from boundary_agent.models import Conversation, Message


async def test_history_keeps_write_order_within_one_transaction():
    # All rows written in one request used to share a timestamp (Postgres now() = transaction start,
    # SQLite = whole seconds), so "the most recent N" was an arbitrary pick among ties.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        conversation = Conversation()
        session.add(conversation)
        await session.flush()
        for i in range(20):
            session.add(Message(conversation_id=conversation.id, role="user", content=f"m{i}"))
            await session.flush()
        history = await AgentRuntime._get_conversation_history(None, session, conversation.id, limit=12)
    await engine.dispose()
    assert [m.content for m in history] == [f"m{i}" for i in range(8, 20)]
