# Demo Notes

## Local Startup
From the repository root:

- Offline (no keys): `uv sync && npm --prefix apps/dashboard install && uv run boundary dev --demo`
- With a real model: copy `.env.example` to `.env` (repo root), set `LLM_MODEL` and that provider's key,
  start `docker compose --env-file .env -f infra/docker-compose.yml up -d`, then `uv run boundary dev`.
- `uv run boundary status` prints the model, database and guard policies that will be used.

## Recommended Demo Flow
1. Open `MCP Servers` and show both seeded servers:
   - `local-sandbox`
   - `exa`
2. Refresh the servers and show that the discovered tool catalog contains tools from both.
3. Open `Chat` and send:
   - `search the web for boundary-ai`
4. Show the Exa tool call completing through the guarded agent.
5. Open `Policies` and create:
   - `require_approval` for `write_file`
   - `validate_args` for `write_file` with `notes/` allowlist
   - `block_tool` for `delete_file`
6. Open `Chat` and send:
   - `list files`
   - `write file notes/demo.txt: hello from the guarded agent`
7. Show the write pausing for approval and the composer disabling for that conversation.
8. While the approval is pending, create a higher-priority `block_tool` rule for `write_file`.
9. Open `Approvals` and approve the pending tool call.
10. Return to `Chat` and show the resumed tool call being blocked because policy changed after approval.
11. Disable the temporary `write_file` block rule, send the write again, and approve it successfully.
12. Send:
   - `delete file notes/demo.txt`
13. Show the tool call getting blocked.
14. Open `Audit Logs` and walk through:
   - dual-server tool discovery
   - policy decision
   - approval request
   - approval invalidation after a live policy change
   - approval decision after the second attempt
   - local and remote tool results
15. Mention that if the approver goes offline, the request expires automatically and the run is denied on TTL.

## Sample Policy Payloads
- Block deletes:
```json
{
  "name": "Block deletes",
  "rule_type": "block_tool",
  "target_tool": "delete_file",
  "priority": 200,
  "conditions": {},
  "action": {
    "reason": "File deletion is disabled in this demo."
  }
}
```

- Require approval for writes:
```json
{
  "name": "Approve writes",
  "rule_type": "require_approval",
  "target_tool": "write_file",
  "priority": 150,
  "conditions": {},
  "action": {
    "reason": "Writes require explicit human review."
  }
}
```

- Restrict writable paths:
```json
{
  "name": "Sandbox notes only",
  "rule_type": "validate_args",
  "target_tool": "write_file",
  "priority": 180,
  "conditions": {
    "path_arg": "path",
    "allow_prefixes": ["notes/"]
  },
  "action": {}
}
```

## Remote MCP Notes
- The backend supports `sse` and `streamable_http` MCP transports.
- Exa hosted MCP is seeded by default through:
  - `EXA_MCP_ENABLED=true`
  - `EXA_MCP_URL=https://mcp.exa.ai/mcp`
- If you have an Exa API key, set:
  - `EXA_API_KEY=<your key>`
- Without a key, Exa still works anonymously for lightweight demos, subject to remote service limits.

## Notes for Review
- The reviewed runtime path is the OpenAI-compatible planner with live MCP discovery.
- `LLM_PROVIDER=mock` is still available for local-only demos, but only when `ALLOW_DEMO_MOCK_PLANNER=true`.
