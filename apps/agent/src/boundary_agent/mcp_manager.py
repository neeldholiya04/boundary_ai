from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from boundary_agent.config import REPO_ROOT
from boundary_agent.models import DiscoveredTool, MCPServer
from boundary_agent.types import ToolDescriptor

logger = logging.getLogger("boundary.mcp")


def describe_error(exc: BaseException) -> str:
    """The innermost real error. The MCP client runs on anyio task groups, so a failed HTTP call
    surfaces as "unhandled errors in a TaskGroup (1 sub-exception)"; unwrap it to e.g.
    "HTTPStatusError: Client error '429 Too Many Requests' ..."."""
    leaf = exc
    while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
        leaf = leaf.exceptions[0]
    return f"{type(leaf).__name__}: {leaf}" if str(leaf) else type(leaf).__name__


class MCPToolError(RuntimeError):
    """A tool call or discovery failed; the message is the unwrapped cause."""


class MCPManager:
    def __init__(self) -> None:
        self.repo_root = REPO_ROOT
        # Credentials from .env, added to a seeded server's requests at connect time so they are
        # never written to the database or returned by the API. Keyed by server name.
        self._runtime_headers: dict[str, dict[str, str]] = {}

    async def ensure_seed_servers(
        self,
        session: AsyncSession,
        *,
        python_executable: str,
        exa_enabled: bool,
        exa_url: str,
        exa_api_key: str | None,
        remote_url: str | None,
        remote_transport: str,
        remote_name: str,
    ) -> None:
        sandbox_config: dict[str, Any] = {
            "command": python_executable,
            "args": ["-m", "boundary_mcp.server"],
            "cwd": str(self.repo_root),
        }
        # The stdio client hands the child only a few variables (HOME, PATH, ...), so pass the
        # sandbox root on explicitly. Without it the image's server falls back to a folder inside
        # the read-only code tree and every write fails instead of landing in the mounted volume.
        sandbox_root = os.getenv("BOUNDARY_SANDBOX_ROOT")
        if sandbox_root:
            sandbox_config["env"] = {"BOUNDARY_SANDBOX_ROOT": sandbox_root}
        await self._ensure_server(session, name="local-sandbox", transport="stdio", config=sandbox_config)

        if exa_enabled:
            exa_config: dict[str, Any] = {"url": exa_url}
            if exa_api_key:
                self._runtime_headers["exa"] = {"Authorization": f"Bearer {exa_api_key}"}
            await self._ensure_server(
                session,
                name="exa",
                transport="streamable_http",
                config=exa_config,
            )

        if remote_url:
            await self._ensure_server(
                session,
                name=remote_name,
                transport=remote_transport,
                config={"url": remote_url},
            )
        await session.commit()

    async def _ensure_server(
        self, session: AsyncSession, *, name: str, transport: str, config: dict[str, Any]
    ) -> None:
        existing = await session.scalar(select(MCPServer).where(MCPServer.name == name))
        if existing:
            existing.transport = transport
            existing.config_json = config
            return
        session.add(MCPServer(name=name, transport=transport, enabled=True, config_json=config))

    def _headers(self, server: MCPServer) -> dict[str, str] | None:
        headers = {**(server.config_json.get("headers") or {}), **self._runtime_headers.get(server.name, {})}
        return headers or None

    @asynccontextmanager
    async def open_session(self, server: MCPServer):
        stack = AsyncExitStack()
        try:
            if server.transport == "stdio":
                params = StdioServerParameters(
                    command=server.config_json["command"],
                    args=server.config_json.get("args", []),
                    cwd=server.config_json.get("cwd"),
                    env=server.config_json.get("env"),
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            elif server.transport == "sse":
                read, write = await stack.enter_async_context(
                    sse_client(server.config_json["url"], headers=self._headers(server))
                )
            elif server.transport == "streamable_http":
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(server.config_json["url"], headers=self._headers(server))
                )
            else:
                raise ValueError(f"Unsupported transport: {server.transport}")

            client = await stack.enter_async_context(ClientSession(read, write))
            await client.initialize()
            yield client
        finally:
            await stack.aclose()

    async def refresh_server_tools(self, session: AsyncSession, server: MCPServer) -> list[ToolDescriptor]:
        try:
            async with self.open_session(server) as client:
                result = await client.list_tools()
        except Exception as exc:
            detail = describe_error(exc)
            logger.warning("tool discovery failed on %s: %s", server.name, detail, exc_info=exc)
            server.last_error = detail
            await session.flush()
            raise MCPToolError(detail) from exc

        await session.execute(delete(DiscoveredTool).where(DiscoveredTool.server_id == server.id))
        tool_descriptors: list[ToolDescriptor] = []
        for tool in result.tools:
            descriptor = ToolDescriptor(
                server_id=server.id,
                server_name=server.name,
                transport=server.transport,
                name=tool.name,
                description=getattr(tool, "description", None),
                input_schema=getattr(tool, "inputSchema", None),
            )
            tool_descriptors.append(descriptor)
            session.add(
                DiscoveredTool(
                    server_id=server.id,
                    name=descriptor.name,
                    description=descriptor.description,
                    input_schema=descriptor.input_schema,
                )
            )

        server.last_discovered_at = datetime.utcnow()
        server.last_error = None
        await session.flush()
        return tool_descriptors

    async def list_tools(self, session: AsyncSession, *, refresh: bool = True) -> list[ToolDescriptor]:
        servers = (
            await session.scalars(
                select(MCPServer)
                .where(MCPServer.enabled.is_(True))
                .options(selectinload(MCPServer.tools))
                .order_by(MCPServer.name)
            )
        ).all()
        descriptors: list[ToolDescriptor] = []
        for server in servers:
            if refresh or not server.tools:
                try:
                    descriptors.extend(await self.refresh_server_tools(session, server))
                except Exception as exc:
                    server.last_error = str(exc)
                    await session.flush()
            else:
                for tool in server.tools:
                    descriptors.append(
                        ToolDescriptor(
                            server_id=server.id,
                            server_name=server.name,
                            transport=server.transport,
                            name=tool.name,
                            description=tool.description,
                            input_schema=tool.input_schema,
                        )
                    )
        return descriptors

    async def call_tool(
        self,
        session: AsyncSession,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        server = await session.get(MCPServer, server_id)
        if server is None:
            raise ValueError(f"Unknown MCP server: {server_id}")

        try:
            async with self.open_session(server) as client:
                result = await client.call_tool(tool_name, arguments)
        except Exception as exc:
            detail = describe_error(exc)
            logger.warning("tool %s on %s failed: %s", tool_name, server.name, detail, exc_info=exc)
            server.last_error = detail
            await session.flush()
            raise MCPToolError(detail) from exc

        server.last_error = None
        await session.flush()

        normalized = result.model_dump(mode="json")
        structured = normalized.get("structuredContent")
        if structured is not None:
            return structured

        text_parts = []
        for item in normalized.get("content", []):
            text = item.get("text")
            if text:
                text_parts.append(text)
        return {"content": "\n".join(text_parts), "raw": normalized}
