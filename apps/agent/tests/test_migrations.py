from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from boundary_agent import (
    migrations,
    models,  # noqa: F401  (registers the tables on Base)
)
from boundary_agent.db import Base


def _columns(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


@pytest.mark.anyio
async def test_upgrade_adds_guard_columns_to_an_old_database() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Simulate a database created before Phase 5.
        for table, column, _ in migrations.ADD_COLUMNS:
            await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        assert "tainted" not in await conn.run_sync(_columns, "runs")

        applied = await conn.run_sync(migrations.upgrade)
        assert set(applied) == {f"{t}.{c}" for t, c, _ in migrations.ADD_COLUMNS}
        assert {"tainted", "taint_reason", "response_schema"} <= await conn.run_sync(_columns, "runs")

        # Existing rows get the defaults; running again is a no-op.
        await conn.execute(
            text("INSERT INTO conversations (id, title, spent_tokens, spent_cost) VALUES ('c', 't', 0, 0)")
        )
        await conn.execute(
            text("INSERT INTO runs (id, conversation_id, status) VALUES ('r', 'c', 'running')")
        )
        tainted = (await conn.execute(text("SELECT tainted FROM runs WHERE id = 'r'"))).scalar_one()
        assert tainted in (0, False)
        assert await conn.run_sync(migrations.upgrade) == []
    await engine.dispose()


@pytest.mark.anyio
async def test_disabled_policies_from_before_modes_become_off() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE policies DROP COLUMN mode"))
        await conn.execute(
            text(
                "INSERT INTO policies (id, name, rule_type, enabled, priority) VALUES "
                "('on', 'a', 'block_tool', 1, 100), ('off', 'b', 'block_tool', 0, 100)"
            )
        )
        assert "policies.mode" in await conn.run_sync(migrations.upgrade)
        modes = dict((await conn.execute(text("SELECT id, mode FROM policies"))).all())
        assert modes == {"on": "enforce", "off": "off"}
    await engine.dispose()
