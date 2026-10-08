# boundary-dashboard

The web UI (Next.js 15, React 19). One sign-in page; what you get depends on your role:

| Role | Pages |
|---|---|
| `user` | `/chat`: a full-screen chat with the agent and the account's own conversation history |
| `admin` | `/guardrails` (policies, modes, text and tool rules), `/approvals`, `/logs`, `/mcp-servers` (Tools), `/playground` |

The role only decides which pages are shown; the agent API checks the token and role on every call.
Guard notices (blocks, approval waits) are shown in the chat as plain sentences with a run reference.

## Run

From the repo root:

```bash
npm --prefix apps/dashboard install
uv run boundary dev            # API on :8000 and this dashboard on :3000
uv run boundary web            # only the dashboard, against a running API (--api-url to change it)
```

Local accounts (when `AUTH_USERS` is unset): `admin` / `admin123`, `user` / `user123`.

- `NEXT_PUBLIC_API_BASE_URL` is the API's address (default `http://127.0.0.1:8000`; `boundary dev`
  sets it). Only `NEXT_PUBLIC_*` values are read from the root `.env` (`next.config.mjs`).
- In the Docker image (`Dockerfile`, built from the repo root) it is empty, so the browser calls
  `/api/...` on the same host and Caddy routes it to the agent.

## Test

There are no unit tests. CI runs `npm ci && npm run build`, which includes the TypeScript check.

## Key files

| Path | What |
|---|---|
| `app/login/page.tsx` | sign-in |
| `app/chat/page.tsx` | the user chat, conversation list, guard notices |
| `app/guardrails/page.tsx` | policies with mode switches and stats, the "New rule" chooser |
| `components/rule-editor.tsx` | text rules: keywords, pattern, topic, plain-language policy; Test (dry run) |
| `components/tool-rule-editor.tsx` | tool rules: ask before running, block, keep paths in a folder, react to flagged runs, budgets |
| `components/mode-switch.tsx` | off / shadow / enforce |
| `app/approvals/page.tsx`, `app/logs/page.tsx`, `app/mcp-servers/page.tsx`, `app/playground/page.tsx` | the other admin pages |
| `components/dashboard-shell.tsx` | navigation and role-based redirects |
| `lib/auth.ts` | the session (token, username, role) in `localStorage` |
| `lib/api.ts` | API calls with the bearer token |
