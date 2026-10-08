# Observability and the playground

Phase 10 adds three things to the agent: **traces** (Langfuse), **metrics** (Prometheus, with a
committed Grafana dashboard), and a **public playground** with abuse controls. Everything here is
optional: with no Langfuse keys, tracing is a no-op; Prometheus and Grafana only run when you ask
for them.

## Tracing (Langfuse)

Turn it on by putting a Langfuse project's keys in the root `.env`:

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com   # or your self-hosted URL
```

`uv run boundary status` does not print the keys; `GET /api/guard/status` reports `"tracing": true`
once they're picked up. Each chat response then carries a `trace_url`, and the Chat page shows a
"Last run's trace" link.

**One trace per run.** The trace id is derived from the run id, so a run that pauses for a human
approval and resumes later stays in the same trace.

```
chat | approval                     agent: the run (input = guarded user text, output = answer)
  planner                           generation: model, prompt, output, tokens, cost (one per call)
  tool.<name>                       tool: arguments, and the result the planner was given
    guard.tool_output               guardrail: redacted excerpt, overall + would-be action
      <policy_id>                   guardrail: score, threshold, action, mode, latency, error
  guard.user_input / guard.final_output / guard.tool_args   (same shape)
```

Trace metadata carries the guard `config_hash` (updated when a mode is switched on the Guardrails
page) and `policy_version`; the conversation id is the Langfuse session.

**Privacy rule: a trace never holds more than the LLM provider sees.** Inputs and outputs are the
guard's output: redacted user text, redacted excerpts for guard spans, and the redacted or withheld
tool result. The raw user message and raw tool output never leave the process. This is tested
end to end (`packages/eval/tests/test_telemetry_trace.py`: a secret in the user's message is absent
from every exported span; its `<CODE_1>` placeholder is present).

## Metrics (Prometheus)

The agent serves `GET /metrics`. Guard metrics come from the library itself
(`boundary_guard.metrics.PrometheusSink`, usable by any app that wraps the guard); agent metrics
from `boundary_agent.telemetry`.

| Metric | Labels | What |
|---|---|---|
| `guard_checks_total` | source, stage, action | blocking checks by overall action |
| `guard_decisions_total` | source, policy, stage, action, would_action, mode | every policy decision |
| `guard_would_block_total` | source, policy, stage | would block if enforced (shadow included) |
| `guard_latency_seconds` | source, policy, stage | histogram |
| `guard_errors_total` | source, policy, stage | detector errors and timeouts |
| `guard_async_queue_depth` | | async checks scheduled but not finished |
| `llm_requests_total` / `llm_tokens_total` / `llm_cost_usd_total` | model, purpose (+ outcome / kind) | real provider calls only |
| `agent_runs_total` / `agent_run_latency_seconds` | status | per run (or resumed part of one) |

`source` keeps traffic apart: `app` (real agent runs), `playground` (pasted scans), `warmup` (one
check per stage at startup). Labels never hold text, scores or matched content.

### Local Prometheus + Grafana

```bash
docker compose --env-file .env -f infra/docker-compose.yml --profile observability up -d
```

- Prometheus on `127.0.0.1:${PROMETHEUS_PORT:-9090}`, scraping the agent on `${AGENT_PORT:-8000}`
  (set both in `.env` if those ports are taken on your machine).
- Grafana on `127.0.0.1:${GRAFANA_PORT:-3001}`, anonymous read-only, with the
  **boundary-ai: guard & agent** dashboard provisioned from
  `infra/observability/grafana/dashboards/boundary-guard.json`: runs, block rate, shadow-only
  would-blocks, LLM spend, async backlog; block rate by stage; shadow vs enforce by policy;
  escalations and redactions; errors and timeouts; p50/p99 per policy; a latency heatmap; run
  latency; cost per run; tokens; and playground traffic on its own panel.

## Playground

A **Playground** page in the dashboard, backed by three endpoints:

| Endpoint | What |
|---|---|
| `POST /api/guard/scan` `{stage, text}` | runs one guard stage over pasted text: per-policy verdict, score, threshold, highlighted spans, latency, and the redacted text. No LLM, nothing stored or traced. |
| `GET /api/playground/scenarios` | the built-in poisoned scenarios (from `packages/eval/scenarios`) |
| `POST /api/playground/attack` `{scenario_id}` or `{page}` | runs the real agent twice, **no defence** vs **guard on (filters + taint)**, against the fixture tool server, side by side: tool calls (unexpected side effects highlighted), guard flags, final answer, trace link |

**Abuse controls.**

- Inputs over `PLAYGROUND_MAX_INPUT_CHARS` (8,000) are refused (413).
- Per-client rate limits: `PLAYGROUND_SCANS_PER_MINUTE` (30) and `PLAYGROUND_ATTACKS_PER_HOUR` (10),
  in Redis when configured. One attack run at a time.
- Attack mode only uses the fixture tool server: fake read-only content and in-memory "writes", with
  no Exa, no real files, and no email.
- Built-in scenarios **replay the recorded cassette**, so they're free, deterministic and need no key.
  A pasted page uses the live model and goes through the global daily cap `LLM_DAILY_BUDGET_USD`
  (0 = off). When the cap is reached, live runs get a 429 that points to the built-ins.
  `PLAYGROUND_LIVE_RUNS=false` turns live runs off entirely.
- Playground runs are traced (tagged `playground`) but kept out of the app's metrics, so demo
  traffic and replayed calls never skew the real panels.

## In production (Phase 12)

The deployment ([DEPLOY.md](DEPLOY.md)) closes the items Phase 10 left open:

- **Nothing is public without a sign-in.** Both hosts serve the app; the agent checks every API call
  against the account's role (user: chat; admin: dashboard and playground). See ARCHITECTURE.md,
  "Sign-in and roles".
- **`/metrics` is never served through the proxy.** Prometheus scrapes the agent on the internal
  network, and Grafana is reachable only through an SSH tunnel.
- **Real client IPs:** the agent trusts the forwarded address from Caddy (the only thing that can
  reach it), so the rate limits apply per real visitor.
- Grafana Cloud remote-write remains an option for alerting from outside the server.

## Known issue on small machines

On a host short of RAM, the guard's models get paged out between requests. The first check after
an idle spell can then exceed its timeout, and fail-closed policies block. The agent warms every
stage at startup, which fixes a true cold start, but it can't stop the OS evicting pages later. See
the memory notes in [EVAL.md](EVAL.md) and give the deployment host enough memory (Phase 12).
