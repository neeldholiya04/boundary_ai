# boundary-sandbox-mcp

The notes workspace the agent reads and writes: an MCP server over stdio that keeps every path inside
one folder. The agent starts it itself (it is seeded as the `local-sandbox` server on the Tools page).

## Tools

| Tool | Effect |
|---|---|
| `list_files(path=".")` | list files and folders |
| `read_file(path)` | read a UTF-8 text file |
| `write_file(path, content, create_dirs=True)` | write a file (mutating) |
| `delete_file(path)` | delete a file (mutating, destructive) |
| `search_files(query, path=".")` | search text across files |

A path that leaves the root is refused. Approvals and taint for writes and deletes are the agent's
job (policy engine), not this server's.

## Run

```bash
uv run python -m boundary_mcp.server        # stdio server
uv run python -m boundary_mcp.probe         # starts it, lists tools, writes and reads probe.txt
```

The root defaults to `apps/sandbox-mcp/sandbox`; set `BOUNDARY_SANDBOX_ROOT` to change it (the
deployment mounts a volume there).

## Test

```bash
uv run pytest apps/sandbox-mcp/tests
```

## Key files

- `src/boundary_mcp/server.py`: the MCP tools
- `src/boundary_mcp/workspace.py`: path checks and file operations
- `src/boundary_mcp/probe.py`: a manual smoke check
