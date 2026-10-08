# boundary-agent

The agent and its HTTP API (FastAPI): sign-in and roles, the chat, the agent loop with the guard at
four stages, the tool-call policy engine, approvals, MCP tool discovery, dashboard rules, the admin
Playground, the audit log, metrics and traces. It also provides the `boundary` command.

## Run

From the repo root (one `.env` there configures everything; see `.env.example`):

```bash
uv run boundary dev --demo     # API + dashboard, mock planner, SQLite, no keys
uv run boundary serve          # API only (--reload, --port, --policy FILE, --no-guard, --demo)
uv run boundary status         # model, database, guard policy version and policies
```

- Accounts: `AUTH_USERS` (`name:password:role`, role `user` or `admin`); unset locally means
  `admin`/`admin123` and `user`/`user123`. `AUTH_SECRET` signs tokens; unset means a random key per
  start, so logins end on restart.
- Guard: `GUARD_POLICY_PATH` (default `policies/guard.yaml`; empty runs unguarded).
- Database: `DATABASE_URL` (Postgres by default, SQLite works); schema changes apply on start
  (`migrations.py`). `REDIS_URL` is optional (live events).
- Model: `LLM_MODEL` (any LiteLLM model string) and the provider's key.

API reference: [docs/API.md](../../docs/API.md). Design: [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md),
[docs/AGENT.md](../../docs/AGENT.md).

## Test

```bash
uv run pytest apps/agent/tests
```

`conftest.py` turns the sign-in check off; tests marked `auth` run with it on.

## Key files (`src/boundary_agent/`)

| File | What |
|---|---|
| `main.py` | app assembly: sign-in middleware, CORS, routers |
| `services.py` | runtime objects built once (settings, guard, policy engine, MCP manager, authenticator, …) |
| `startup.py` | start and stop: database, MCP servers, default tool rules, stored rules and mode overrides, guard warm-up, approval sweeper |
| `auth.py` | accounts, tokens, and `required_role(path)`: anything under `/api` not listed for users is admin-only |
| `api/` | routers: `auth`, `chat`, `approvals`, `guard`, `guard_rules`, `tool_policies`, `tools`, `logs`, `system` |
| `agent.py` | the agent loop: planner, tool calls, approvals, secret handling |
| `guarding.py` | the guard adapter: checks at four stages, taint, the plain messages users see |
| `policy.py` | the tool-call policy engine (off / shadow / enforce) |
| `rules.py` | dashboard text rules: spec, compilation into guard policies, dry runs |
| `playground.py` | the admin Playground API (scan and attack mode, rate limits, budget) |
| `llm.py` | the LiteLLM planner |
| `mcp_manager.py` | MCP servers and tool discovery |
| `cli.py` | the `boundary` command |
