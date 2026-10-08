"""MCP servers and the tools they offer."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from boundary_agent import services
from boundary_agent.db import get_session
from boundary_agent.models import MCPServer
from boundary_agent.schemas import MCPServerCreate, MCPServerUpdate
from boundary_agent.secrets_mask import mask_config, unmask_config

router = APIRouter(prefix="/api/mcp")


@router.get("/servers")
async def list_mcp_servers(session: AsyncSession = Depends(get_session)) -> list[dict]:
    servers = (
        await session.scalars(
            select(MCPServer).options(selectinload(MCPServer.tools)).order_by(MCPServer.name.asc())
        )
    ).all()
    return [
        {
            "id": server.id,
            "name": server.name,
            "transport": server.transport,
            "enabled": server.enabled,
            "config": mask_config(server.config_json),
            "last_error": server.last_error,
            "last_discovered_at": server.last_discovered_at,
            "tool_count": len(server.tools),
            "status": _server_status(server),
        }
        for server in servers
    ]


@router.post("/servers")
async def create_mcp_server(payload: MCPServerCreate, session: AsyncSession = Depends(get_session)) -> dict:
    server = MCPServer(
        name=payload.name,
        transport=payload.transport,
        enabled=payload.enabled,
        config_json=payload.config,
    )
    session.add(server)
    await session.flush()
    await services.audit_logger.record(
        session, "mcp.server_created", {"server_id": server.id, "name": server.name}
    )
    await session.commit()
    return {"id": server.id}


@router.patch("/servers/{server_id}")
async def update_mcp_server(
    server_id: str, payload: MCPServerUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    server = await session.get(MCPServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if payload.enabled is not None:
        server.enabled = payload.enabled
    if payload.config is not None:
        server.config_json = unmask_config(payload.config, server.config_json)
    await services.audit_logger.record(
        session,
        "mcp.server_updated",
        {"server_id": server.id, "enabled": server.enabled},
    )
    await session.commit()
    return {"ok": True}


@router.post("/servers/{server_id}/refresh")
async def refresh_mcp_server(server_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    server = await session.get(MCPServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    try:
        tools = await services.mcp_manager.refresh_server_tools(session, server)
        await services.audit_logger.record(
            session,
            "mcp.server_refreshed",
            {"server_id": server.id, "tool_count": len(tools)},
        )
        await session.commit()
        return {"tool_count": len(tools)}
    except Exception as exc:
        server.last_error = str(exc)
        await session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/tools")
async def list_mcp_tools(session: AsyncSession = Depends(get_session)) -> list[dict]:
    tools = await services.mcp_manager.list_tools(session, refresh=False)
    return [
        {
            "server_id": tool.server_id,
            "server_name": tool.server_name,
            "transport": tool.transport,
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in tools
    ]


def _server_status(server: MCPServer) -> str:
    if not server.enabled:
        return "disabled"
    if server.last_error:
        lowered = server.last_error.lower()
        if "401" in lowered or "403" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
            return "auth_error"
        if server.last_discovered_at:
            return "execution_failed"
        return "discovery_failed"
    if server.last_discovered_at and server.tools:
        return "connected"
    return "pending"
