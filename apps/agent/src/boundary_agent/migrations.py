"""Idempotent schema upgrades for databases created before a column existed.

`Base.metadata.create_all` creates missing *tables* but never alters existing ones, so columns
added to existing tables are applied here after it runs. Every step checks the live schema first,
so running this on every startup is safe. SQLite cannot drop NOT NULL in place; an old local
SQLite file keeps `approval_requests.server_id NOT NULL` (delete the file to pick up the change).
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

logger = logging.getLogger("boundary.migrations")

# (table, column, DDL type/default). Defaults are required for NOT NULL columns on existing rows.
ADD_COLUMNS: list[tuple[str, str, str]] = [
    ("runs", "tainted", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("runs", "taint_reason", "TEXT"),
    ("runs", "response_schema", "VARCHAR(80)"),
    ("approval_requests", "kind", "VARCHAR(24) NOT NULL DEFAULT 'tool_call'"),
    ("approval_requests", "stage", "VARCHAR(24)"),
    ("policies", "mode", "VARCHAR(16) NOT NULL DEFAULT 'enforce'"),
]


def upgrade(conn: Connection) -> list[str]:
    applied: list[str] = []
    inspector = inspect(conn)
    for table, column, ddl in ADD_COLUMNS:
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column not in existing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            applied.append(f"{table}.{column}")

    if "policies.mode" in applied:
        # Before modes, a disabled policy was simply off.
        conn.execute(text("UPDATE policies SET mode = 'off' WHERE NOT enabled"))

    if conn.dialect.name == "postgresql":
        server_id = next(c for c in inspector.get_columns("approval_requests") if c["name"] == "server_id")
        if not server_id["nullable"]:
            conn.execute(text("ALTER TABLE approval_requests ALTER COLUMN server_id DROP NOT NULL"))
            applied.append("approval_requests.server_id nullable")

    for step in applied:
        logger.info("schema upgrade applied: %s", step)
    return applied
