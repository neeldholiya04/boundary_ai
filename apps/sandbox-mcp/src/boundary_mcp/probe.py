from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run_probe() -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[4]
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "boundary_mcp.server"],
        cwd=str(repo_root),
    )

    async with stdio_client(server_params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        await session.call_tool("write_file", {"path": "probe.txt", "content": "hello from probe"})
        read_result = await session.call_tool("read_file", {"path": "probe.txt"})
        return {
            "tool_count": len(tools.tools),
            "tool_names": [tool.name for tool in tools.tools],
            "read_result": read_result.model_dump(mode="json"),
        }


def main() -> None:
    result = asyncio.run(run_probe())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
