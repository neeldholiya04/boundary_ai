# Custom MCP Server

Sandboxed file workspace MCP server used by the guarded agent demo.

## Tools
- `list_files`
- `read_file`
- `write_file`
- `delete_file`
- `search_files`

## Run
- `python -m boundary_mcp.server`

## Sandbox Root
- Defaults to `apps/sandbox-mcp/sandbox`
- Override with `BOUNDARY_SANDBOX_ROOT`
