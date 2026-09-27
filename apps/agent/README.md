# API

FastAPI control plane for the guarded MCP agent.

## Responsibilities
- serve the chat and admin APIs
- manage MCP server discovery and execution
- evaluate tool intents through the policy engine
- orchestrate approvals and audit logs
- expire stale approvals in the background
- publish live events through Redis pub/sub when configured

## Run
- From the repo root: `uv run boundary serve --reload` (or `uv run boundary serve --demo` without keys)

## Reviewed Defaults
- `LLM_PROVIDER=litellm`, `LLM_MODEL=openai/gpt-4.1-mini` (any LiteLLM model string works)
- `GUARD_POLICY_PATH=policies/guard.yaml`
- `ALLOW_DEMO_MOCK_PLANNER=false`
- `DATABASE_URL=postgresql+asyncpg://boundary:boundary@localhost:5432/boundary`
- `REDIS_URL=redis://localhost:6379/0`
