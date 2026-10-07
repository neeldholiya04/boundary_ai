# boundary-ai

A research-assistant agent with guardrails built into its loop, plus the eval harness that proves
the guardrails work. The agent discovers tools from MCP servers, checks every tool call against a
deterministic policy engine, asks a human to approve sensitive actions, and runs the guard at four
points: user input, outgoing tool arguments, tool output, and the final answer. Every guard
policy's catch rate, false-positive rate, latency and cost are measured in CI.

New here? Start with [PROJECT.md](PROJECT.md). The spec is [IDEA.md](docs/IDEA.md), the build plan
[PLAN.md](docs/PLAN.md), and where each part came from is in [CREDITS.md](CREDITS.md).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and, for the dashboard, Node.js 22+. Everything runs
from the repository root.

```bash
uv sync                                  # Python workspace
npm --prefix web install                 # dashboard
uv run boundary dev --demo               # agent API on :8000 + dashboard on :3000, no keys needed
```

`--demo` uses a mock planner, a local SQLite file and no network MCP server, so the whole loop
(guard, policies, approvals, dashboard) runs offline. For the real thing:

1. `cp .env.example .env` and pick the model. This one file at the root configures the agent,
   the dashboard and the deployment. The planner goes through
   [LiteLLM](https://docs.litellm.ai/), so switching provider is two lines:
   - `LLM_MODEL=openai/gpt-4.1-mini` + `OPENAI_API_KEY=...` (default)
   - `LLM_MODEL=anthropic/claude-haiku-4-5-20251001` + `ANTHROPIC_API_KEY=...`
   - `LLM_MODEL=gemini/<model>` + `GEMINI_API_KEY=...`
   - `LLM_MODEL=ollama/<model>` + `LLM_API_BASE=http://localhost:11434`
2. Start Postgres and Redis: `docker compose --env-file .env -f infra/docker-compose.yml up -d` (or set
   `DATABASE_URL=sqlite+aiosqlite:///./boundary.db` and leave `REDIS_URL` empty).
3. `uv run boundary status` to check what will run, then `uv run boundary dev`.

The production policies use a gated model (Llama Prompt Guard 2): request access on its Hugging
Face page, then `uv run hf auth login` (or set `HF_TOKEN`). The first run downloads ~2 GB of
pinned models into `~/.cache/huggingface`.

| Command | What |
|---|---|
| `uv run boundary serve` | the agent API (`--reload`, `--port`, `--demo`, `--policy FILE`, `--no-guard`) |
| `uv run boundary web` | the dashboard against a running API |
| `uv run boundary dev` | both together |
| `uv run boundary status` | model, database, and the guard policies with their mode and action |
| `docker compose --env-file .env -f infra/docker-compose.yml --profile observability up -d` | local Prometheus + Grafana with the guard dashboard ([observability](docs/OBSERVABILITY.md)) |

Guard settings live in `.env`: `GUARD_POLICY_PATH` (default `policies/guard.yaml`; empty runs
the agent unguarded), `GUARD_TOOL_OUTPUT_ON_BLOCK` (`continue` | `halt`), `GUARD_SPOTLIGHT`,
`GUARD_TAINT_LABELS`, `SEED_GUARD_SIGNAL_POLICIES`. Schema changes are applied on startup
(`apps/agent/src/boundary_agent/migrations.py`). A walkthrough of the demo is in [docs/DEMO.md](docs/DEMO.md).

## Layout

```
apps/          what you run
  agent/         the agent: FastAPI runtime, planner, policy engine, approvals, guard hooks, `boundary` CLI
  dashboard/     Next.js UI: chat, policies, approvals, decision log, Guardrails page
  sandbox-mcp/   sandboxed file MCP server the agent launches over stdio
packages/      libraries the apps use
  guard/         boundary_guard: pipeline, policies, detectors (no import of the agent; wraps any LLM app)
  eval/          boundary_eval: datasets, scenarios, baselines, CI gates, detector + end-to-end harnesses
  detector/      boundary_detector: our fine-tuned tool-output injection detector + Colab training
policies/      the guard's versioned policy YAML, rulesets, schemas (CHANGELOG.md)
infra/         docker compose (local Postgres/Redis; Phase 12 deployment stack), Caddyfile
docs/          IDEA and PLAN, threat model, eval, architecture, agent internals, observability, CI, demo
```

## Development

Python 3.12 is pinned in `.python-version`.

```bash
uv sync                                       # install workspace + dev tools
uv run pytest                                 # tests
uv run ruff check . && uv run ruff format .   # lint + format
uv run boundary-eval validate packages/eval/datasets   # check dataset files
uv run boundary-eval detectors --suite golden  # run the detector eval
uv run boundary-eval compare packages/eval/baselines/golden.json packages/eval/results/run.json  # CI gate
uv run boundary-eval tune packages/eval/results/run.json --max-fpr 0.02   # dev threshold sweep
uv run boundary-eval build-extended            # rebuild extended set from pinned sources
```

## Status

- [x] Phase 0: repo, workspace, CI
- [x] Phase 2 (core): pipeline, YAML config, shadow mode, redaction, secrets/jailbreak/schema detectors
- [x] Phase 1: [threat model](docs/THREAT_MODEL.md), [eval design](docs/EVAL.md), golden set v0 (81 records)
- [x] Phase 2.5: agent brought into the repo, LiteLLM planner, observe-only guard on tool output
- [x] Phase 3: eval runner (Wilson CIs, latency bench), baseline, CI gate ([results](docs/EVAL.md#golden-v0-baseline))
- [x] Phase 4: ML detectors (Prompt Guard 2, ProtectAI, Presidio, toxic-bert, MiniLM topic, NLI), extended set (2,406 records), threshold tuning ([results](docs/EVAL.md))
- [x] Phase 5: guard enforced in the agent at all four stages, run taint, content review, spotlighting ([architecture](docs/ARCHITECTURE.md))
- [x] Phase 6: end-to-end scenario harness (fixture MCP, ASR/utility metrics, LLM cassette); with filters + taint, attack success 40% → 20% at 67% benign success (5 attack / 3 benign test scenarios; [results](docs/EVAL.md#end-to-end-results))
- [x] Phase 7: CI gates (detector + e2e), sticky PR comment, nightly cassette refresh ([CI docs](docs/CI.md))
- [x] Phase 8: our fine-tuned detector beats both off-the-shelf baselines on tool-output injection (82.8% catch / 9.5% FPR) ([detector](packages/detector/README.md))
- [x] Phase 9: runtime mode overrides (persisted + audited), shadow-vs-enforce stats, Guardrails dashboard page
- [x] Phase 10: Langfuse tracing (one trace per run, redacted only), Prometheus metrics + Grafana dashboard, public Playground (scan + attack mode) with rate limits and a daily LLM budget ([observability](docs/OBSERVABILITY.md))
- [ ] Phase 11+: see [PLAN.md](docs/PLAN.md)
