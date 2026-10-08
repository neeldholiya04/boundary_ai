import httpx

from boundary_agent.mcp_manager import describe_error


def test_describe_error_unwraps_nested_task_groups():
    request = httpx.Request("POST", "https://mcp.example.test/mcp")
    response = httpx.Response(429, request=request)
    cause = httpx.HTTPStatusError("Client error '429 Too Many Requests'", request=request, response=response)
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("inner", [cause])])
    assert describe_error(wrapped) == "HTTPStatusError: Client error '429 Too Many Requests'"


def test_describe_error_keeps_plain_errors_and_names_empty_ones():
    assert describe_error(ValueError("bad args")) == "ValueError: bad args"
    assert describe_error(TimeoutError()) == "TimeoutError"


async def test_seeded_sandbox_writes_into_the_configured_root(tmp_path, monkeypatch):
    # The stdio client passes the child only HOME, PATH and a few others, so the image's
    # BOUNDARY_SANDBOX_ROOT used to be dropped and the server wrote into the read-only code tree.
    import sys

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from boundary_agent.db import Base
    from boundary_agent.mcp_manager import MCPManager
    from boundary_agent.models import MCPServer

    monkeypatch.setenv("BOUNDARY_SANDBOX_ROOT", str(tmp_path))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    manager = MCPManager()
    async with factory() as session:
        await manager.ensure_seed_servers(
            session,
            python_executable=sys.executable,
            exa_enabled=False,
            exa_url="",
            exa_api_key=None,
            remote_url=None,
            remote_transport="sse",
            remote_name="remote",
        )
        server = await session.scalar(select(MCPServer).where(MCPServer.name == "local-sandbox"))
        async with manager.open_session(server) as client:
            result = await client.call_tool("write_file", {"path": "probe.txt", "content": "hi"})
    await engine.dispose()
    assert not result.isError
    assert (tmp_path / "probe.txt").read_text() == "hi"
