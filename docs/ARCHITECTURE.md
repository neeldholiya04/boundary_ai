# Architecture

This is the high-level design: the parts of the system, how a request flows through them, and where each
decision is made. Detail lives in the linked documents: [API.md](API.md) (every endpoint),
[MODELS.md](MODELS.md) (every detector and model), [AGENT.md](AGENT.md) (the agent loop),
[EVAL.md](EVAL.md) (how it is measured), [DEPLOY.md](DEPLOY.md) and [OBSERVABILITY.md](OBSERVABILITY.md).

## System overview

```
                 ┌──────────────────────────── one host (Caddy, HTTPS) ────────────────────────────┐
 browser ──────► │  dashboard (Next.js)          agent (FastAPI)                                     │
 user / admin    │   /login                       /api/* ──► sign-in middleware (roles)              │
                 │   /chat        (user)          ├─ chat ──► AgentRuntime ──► planner LLM (LiteLLM)  │
                 │   /guardrails…  (admin)        │            │  ▲                                    │
                 │                                │            ▼  │ guard checks at 4 stages          │
                 │                                │       boundary_guard (policies/guard.yaml + rules)│
                 │                                │            │                                      │
                 │                                │       policy engine (tool rules) ──► MCP tools    │
                 │                                ├─ approvals, guard, rules, logs, playground       │
                 │                                └─ Postgres/SQLite · Redis (events, limits)        │
                 └───────────────────────────────────────────────────────────────────────────────────┘
   MCP servers: local sandbox (files) · Exa (web search) · any added in Tools
   Models: Llama Prompt Guard 2, ProtectAI DeBERTa, MiniLM (topic), Presidio (PII), toxic-bert, NLI (groundedness)
   Offline: packages/eval (golden / extended / end-to-end evals, CI gates) · Langfuse + Prometheus/Grafana
```

| Part | Where | Job |
|---|---|---|
| Dashboard | `apps/dashboard` (Next.js) | Sign-in; the chat for user accounts; Guardrails, Approvals, Logs, Tools, Playground for admins |
| Agent | `apps/agent` (FastAPI) | The API, the agent loop, approvals, the policy engine for tool rules, the audit log |
| Guard library | `packages/guard` (`boundary_guard`) | Runs the policies of a stage over text; knows nothing about the agent |
| Policies | `policies/` | `guard.yaml` (the reviewed baseline), rulesets, topic exemplars, schemas, `CHANGELOG.md` |
| Sandbox MCP | `apps/sandbox-mcp` | The file tools the agent uses (list, read, write, delete, search) inside a sandbox folder |
| Eval | `packages/eval` (`boundary_eval`) | Datasets, detector and end-to-end evals, baselines, CI gates, load test, results write-up |
| Detector | `packages/detector` | Our own injection detector (training, model card) |
| Infra | `infra/` | Docker Compose, Caddy, deploy script, smoke test, observability stack |

The one deliberate internal boundary: the guard library knows nothing about the agent, and the agent calls
it through one adapter, `apps/agent/src/boundary_agent/guarding.py`.

### Inside the agent

| Module | Responsibility |
|---|---|
| `main.py` | Builds the app: sign-in middleware, CORS, routers |
| `services.py` | The runtime objects, built once: settings, guard, agent runtime, policy engine, MCP manager, telemetry, limits, authenticator |
| `startup.py` | Lifespan: database, MCP servers and tools, default tool rules, stored mode overrides and dashboard rules, guard warm-up, approval sweeper |
| `api/` | One router per area: `auth`, `chat`, `approvals`, `guard`, `guard_rules`, `tool_policies`, `tools`, `logs`, `system` |
| `agent.py` | `AgentRuntime`: the loop, approvals, content review, the secret stops |
| `guarding.py` | Guard adapter, decision sink, user-facing block messages, secret placeholders |
| `policy.py` | Policy engine for tool rules (block, approval, folders, budgets, taint) |
| `rules.py` | Dashboard rule spec, compilation into guard policies, dry runs |
| `auth.py` | Accounts, signed tokens, `required_role(path)` |
| `playground.py` | Admin playground: scan and attack demo |
| `llm.py` | The planner (LiteLLM), spotlighting of tool output |

## Request flow

1. The browser signs in (`POST /api/auth/login`) and sends `Authorization: Bearer <token>` on every call.
   The middleware checks the path's role before any route runs (see "Sign-in and roles").
2. A user's message (`POST /api/chat`) is checked at **user input**. A secret is redacted and stops the run
   with a fixed answer; a block or review stops it with a plain message.
3. The planner proposes a tool call. It is checked at **tool args** by the guard, then by the policy engine
   (tool rules: block, approval, folders, budgets, taint). It runs only if both allow it.
4. The tool's result is checked at **tool output**. Injection marks the run tainted, so later writes need
   approval.
5. The loop repeats until the planner answers. The answer is checked at **final output**.
6. Every check is recorded (`guard_decisions`, audit events). The dashboard refreshes over server-sent
   events; users' chats poll while a request waits for approval.

```
user ──► USER_INPUT ──► planner ──► TOOL_ARGS ──► policy engine ──► MCP tool ──► TOOL_OUTPUT ──┐
                           ▲                                                                   │
                           └──────────────────────────── (loop) ◄──────────────────────────────┘
                        planner answer ──► FINAL_OUTPUT ──► user
```

## What each verdict does

| Stage | `block` | `redact` | `escalate` |
|---|---|---|---|
| user input | run ends with a plain message (below) | redacted text goes to the planner, the message history, the audit log and the conversation title; a redacted **secret** ends the run (see "Secrets in the user's own message") | content review; the run pauses |
| tool args | tool call blocked (like a policy-engine block) | treated as block: arguments are never rewritten | tool-call approval (the existing approval flow) |
| tool output | the planner gets `{"withheld_by_guard": …}` and the loop continues (`GUARD_TOOL_OUTPUT_ON_BLOCK=halt` ends the run instead) | redacted result; the MCP `raw` copy (which repeats the content) is dropped | content review; approve passes the content on, deny withholds it and the run continues |
| final output | answer withheld | redacted answer | content review; approve releases the answer |

`flag` and shadow-mode decisions change nothing in the run; they are recorded.

**What the user is told.** Never the detector's reasons. A block ends with a plain sentence chosen by what
the stopping policy detects (injection, off-topic, toxicity, secret, personal data) or by the stage, plus a
run reference (`guarding.user_block_message`); a policy or dashboard rule can set its own `message`.
Scores, exemplars, policy ids and keywords stay in the logs and are not given to the model either.
Blocks, stops and approval waits are stored with `metadata.notice`, and the chat shows them as notices.

Guard checks happen **before** anything is persisted, so a withheld or redacted value never reaches
the `messages` table, the audit log, or the SSE stream in raw form (tested in
`apps/agent/tests/test_guard_integration.py`).

## Run taint

When a tool-output policy that detects `injection` fires, **enforced or in shadow**, the run is
marked `tainted` with the reason. The policy engine's `guard_signal` rule type reacts to it:

```json
{"rule_type": "guard_signal", "target_tool": "write_file",
 "conditions": {"run_tainted": true},
 "action": {"verdict": "require_approval", "reason": "..."}}
```

On first start the agent creates this rule for `write_file` and `delete_file`
(`SEED_GUARD_SIGNAL_POLICIES`) and records `policy.defaults_seeded`, so a default an operator deletes
or edits stays that way across restarts. This is how tool-output injection is handled in policy v3:
no off-the-shelf detector is good enough to *block* tool output (docs/EVAL.md), but its verdict is
good enough to make a human approve mutating actions after suspicious content was read. Normal
precedence still applies: explicit `block_tool` rules beat taint approvals.

## Secrets in the user's own message

A redaction tells the model a value existed without giving it the value, and a model left to it will
act on the placeholder: in a live test it wrote `<OPENAI_KEY_1>` into `.env` over the real key and
then said it had written the key. Two deterministic stops prevent that:

- when a secrets policy (`detects: [secret]`) redacts the user's message, the run ends before the
  planner with a fixed answer (the secret was removed before it was read, nothing was done, add it
  yourself), audited as `guard.secret_withheld`;
- a tool call that **assigns** a secret's placeholder as a value (`OPENAI_API_KEY=<OPENAI_KEY_1>`, in any
  case, templating or URL/HTML encoding; labels from those policies' rulesets, or `KNOWN_SECRET`) is
  refused before it runs (`guard.secret_placeholder_blocked`), while a secrets policy enforces. A
  placeholder merely mentioned (a note about a redacted log) and personal-data placeholders (`<EMAIL_1>`)
  still reach tools.

Detection itself has two layers: the ~216 provider formats imported from gitleaks (run on RE2, linear
time) and context rules (a known provider prefix at any length, credential-named `.env` lines). See
[MODELS.md](MODELS.md) and `policies/CHANGELOG.md` (v5 to v9).

## Content review

`escalate` creates an `approval_requests` row with `kind = "content_review"` and the guard stage.
It stores the guard's output (already redacted where a policy redacted), the user message, and the
executed steps, so the run can resume exactly where it paused. Content reviews use the existing TTL
and default-deny on expiry. The Approvals page shows the held content and what approve or deny will do.

## What is stored

- `guard_decisions`: one row per policy per check, with mode, applied action, would-action, score,
  threshold, latency, reasons, span labels, config hash, and a **redacted excerpt** (spans from PII,
  secret and redacting policies replaced; injection text left readable for reviewers) plus the
  sha256 of the checked text. The raw text is never stored.
- `guard.decision` audit events: the same summary without the excerpt, streamed to the dashboard.
- Async policies (e.g. groundedness) write their rows later through `GuardDecisionSink`, using the
  excerpt computed by the blocking check.
- `GET /api/guard/status` (policy set, modes, config hash) and `GET /api/guard/decisions`
  (filterable by run and policy, `fired_only`) serve the dashboard.

## Spotlighting

With `GUARD_SPOTLIGHT=true` the planner prompt wraps each tool result in
`<<untrusted_tool_output NONCE>> … <<end_untrusted_tool_output NONCE>>`, with a fresh random nonce
per request. Marker-like text inside the content is neutralised, and the system prompt tells the
model the delimited text is data, not instructions. It is a prompt-level defence; its effect is
measured separately in the end-to-end eval (Phase 6).

## Runtime controls and shadow-vs-enforce (Phase 9)

A policy's **mode** can be changed while the agent runs, from the Guardrails dashboard page:

- `PATCH /api/guard/policies/{id}` with `{"mode": "off|shadow|enforce"}` calls `guard.set_mode`,
  which recomputes the config hash immediately (the next decision uses the new mode). The change is
  written to the `guard_overrides` table, audited as `guard.mode_changed`, and applied again at
  startup (`apply_overrides`), so it survives a restart. Setting a policy back to its YAML default
  deletes the override.
- `GET /api/guard/stats` returns, per policy over recent decisions, how often it **would have fired**
  and with which action, plus p50/p99 latency and error count. This is the **shadow-vs-enforce**
  view: run a new detector in shadow, watch its would-fire rate and what it would have blocked, then
  flip it to enforce from the UI once you trust it.
- `GET /api/guard/status` lists every policy with its stages, detector, action, execution, live mode,
  threshold, the config hash, and the dropped-async counter.

The A/B form of the delta (attack-success and utility under shadow vs enforced) comes from the
end-to-end harness: `boundary-eval e2e --config shadow --config filters_taint`.

## Guard rules: policies written in the dashboard

`policies/guard.yaml` is the reviewed baseline (secrets, PII, injection, toxicity, ...), measured in CI.
A **rule** is a policy an operator adds on top while the app runs, from the Guardrails page or
`/api/guard/rules`. A rule says:

| | Options |
|---|---|
| where | any of the four stages; on tool stages, optionally only some tools (`tools`) |
| what it checks | `keywords` (whole words, any case), `pattern` (regexes; a 0.25 s search budget per check, repetition counts capped), `topic` (example requests, compared by meaning with the shipped topic model), `llm_judge` (a policy in plain words, judged by an LLM), `always` (every call: tool rules) |
| what happens | flag, redact (keywords/pattern only), escalate to a human, block; tool-output rules can also taint the run (in shadow too, like the injection detectors) |
| mode | off / shadow (the default for a new rule) / enforce |

Each rule compiles (`apps/agent/src/boundary_agent/rules.py`) into one guard policy, `rule_<slug>`, added
to the running guard with `Guard.add_policy`: same pipeline, decision log, stats, metrics and config hash
as the file's policies. File policies can't be replaced or removed by a rule, only switched off.
Rules are stored in `guard_rules` (the spec as written, plus a version), reloaded at startup (a rule that
fails to load is reported on the page, the rest still run), and every change is audited with the full
spec (`guard.rule_created|updated|deleted`), which is each rule's history.

A rule carries its own examples (`should_fire` / `should_pass`). **Test** (`POST /api/guard/rules/test`)
runs a draft against them and against the eval set's benign records at the rule's stages, and reports
the would-fire rate on that clean traffic, before anything is stored. `GET /api/guard/rules/export` writes
the active rules as a policy-file fragment, so a rule that proved itself can be reviewed into
`guard.yaml` and measured in CI like the rest.

### Text rules and tool rules

The dashboard has one "New rule" flow with two kinds, one per engine:

| | Text rule (guard) | Tool rule (policy engine) |
|---|---|---|
| Looks at | what a request, tool result or answer says | a tool call: the tool and its arguments |
| Can | flag, redact, ask a person, block, taint the run | ask before running, block, keep a path inside folders, react to a tainted run, cap tokens or cost |
| Stored in | `guard_rules`, compiled into a guard policy | `policies` (`/api/policies`) |
| Modes | off / shadow / enforce | off / shadow / enforce (shadow verdicts are logged on `policy.decision` as `shadow`) |

Tool rules stay in the policy engine because it owns what the guard doesn't: path normalisation for
folder rules, conversation budgets, the taint → approval link, and re-checking a call when an approval
resumes it. The guard's `always` check can still express "every `send_email` needs approval"; existing
rules of that kind keep working, but new ones are made as tool rules.

Notes:
- An `llm_judge` rule sends the checked text to the judge model's provider (by default the one the
  agent uses; a rule may only name models in `GUARD_JUDGE_MODELS`), costs one call per 12k-character
  chunk, and fails closed if the judge errors or returns something that isn't a verdict. Start it in
  shadow or async. Judged dry runs take at most 10 examples and are rate limited.
- Each rule's history (the audit log) keeps the full spec, including its keywords: treat it like the
  rules themselves. Matches of keyword and pattern rules are left out of stored excerpts.
- Rules are created by admin accounts only (see "Sign-in and roles").

## Sign-in and roles

`apps/agent/src/boundary_agent/auth.py`. Accounts come from `AUTH_USERS` (`name:password:role`); a
sign-in (`POST /api/auth/login`) returns a token signed with `AUTH_SECRET` that expires after
`AUTH_TOKEN_HOURS`. One middleware checks every `/api` request against `required_role(path)`:

| Role | Gets | API |
|---|---|---|
| (signed out) | the sign-in page | `/health`, `/api/auth/login` |
| `user` | a full-screen chat with their own history | `/api/chat`, `/api/conversations…` (only conversations they own; others look missing), `/api/auth/me` |
| `admin` | the dashboard: guardrails, approvals, logs, tools, playground (no chat) | everything else under `/api`, including the event stream and the playground |

Any path not listed for users needs `admin`, so a new endpoint is private until opened on purpose.
Users don't get the live event stream (it carries every audit event) or trace links; their chat polls
while a request waits for approval. The playground is admin-only, so its scans run every policy the
chat runs, operator rules included; the deployment's own keys (known secrets) are still not checked
there, so a scan can't confirm a guess. Failed sign-ins are throttled per client.

## Async checks

Slow checks (e.g. groundedness) run with `execution: async`: the blocking result returns immediately
and the async policies run in a background task, their decisions written later through
`GuardDecisionSink` (its own DB session) and streamed as `guard.async_decision`. A bounded queue
drops (and counts) work under overload rather than blocking the request. The queue and sink are the
seam where Phase 12 can swap in a Redis-backed worker without touching the agent loop.

## Schema changes

`create_all` makes new tables; `migrations.py` adds new columns to existing databases on startup
(idempotent, checked against the live schema). On Postgres it also drops `NOT NULL` from
`approval_requests.server_id`, which content reviews of user input and answers need.

## Observability

Every run is one Langfuse trace (planner generations, tool spans, a guardrail span per guard check
with a child per policy), and the guard's decisions are Prometheus metrics via the library's
`PrometheusSink`. Traces only hold guarded text. See [OBSERVABILITY.md](OBSERVABILITY.md), which
also covers the playground and its abuse controls.

What all of this costs, and how well it works, is measured rather than claimed: see
[RESULTS.md](RESULTS.md) (generated from the committed eval, end-to-end and load-test results) and
[MODELS.md](MODELS.md) (per-detector numbers).
