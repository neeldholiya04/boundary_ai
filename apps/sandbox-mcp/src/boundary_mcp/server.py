from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from boundary_mcp.workspace import SandboxWorkspace, WorkspaceError


def _default_root() -> Path:
    env_root = os.getenv("BOUNDARY_SANDBOX_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parents[2] / "sandbox"


workspace = SandboxWorkspace(_default_root())
mcp = FastMCP("boundary-sandbox")


def _guard(fn_name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except WorkspaceError as exc:
        raise ValueError(f"{fn_name} failed: {exc}") from exc


@mcp.tool(name="list_files", description="List files and folders under the sandbox root.")
def list_files(path: str = ".") -> list[dict[str, Any]]:
    return _guard("list_files", workspace.list_files, path)


@mcp.tool(name="read_file", description="Read a UTF-8 text file inside the sandbox root.")
def read_file(path: str) -> dict[str, Any]:
    return _guard("read_file", workspace.read_file, path)


@mcp.tool(name="write_file", description="Write UTF-8 content into a file inside the sandbox root.")
def write_file(path: str, content: str, create_dirs: bool = True) -> dict[str, Any]:
    return _guard("write_file", workspace.write_file, path, content, create_dirs)


@mcp.tool(name="delete_file", description="Delete a file inside the sandbox root.")
def delete_file(path: str) -> dict[str, Any]:
    return _guard("delete_file", workspace.delete_file, path)


@mcp.tool(name="search_files", description="Search for a text query across sandbox files.")
def search_files(query: str, path: str = ".") -> dict[str, Any]:
    return _guard("search_files", workspace.search_files, query, path)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
