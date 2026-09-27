# Architecture: how the guard and the agent fit together

boundary-ai is one application with one deliberate internal boundary: the guardrails library
(`boundary_guard`) knows nothing about the agent, and the agent (`apps/agent/`) calls it through one adapter,
`apps/agent/src/boundary_agent/guarding.py`, at four points of its loop in `agent.py`:

```
user ──► USER_INPUT ──► planner ──► TOOL_ARGS ──► policy engine ──► MCP tool ──► TOOL_OUTPUT ──┐
                           ▲                                                                   │
                           └──────────────────────────── (loop) ◄──────────────────────────────┘
                        planner answer ──► FINAL_OUTPUT ──► user
```

## What each verdict does

| Stage | `block` | `redact` | `escalate` |
|---|---|---|---|
| user input | run ends: "Request blocked by the guard" | redacted text goes to the planner, the message history, the audit log and the conversation title | content review; the run pauses |
| tool args | tool call blocked (like a policy-engine block) | treated as block: arguments are never rewritten | tool-call approval (the existing approval flow) |
| tool output | the planner gets `{"withheld_by_guard": …}` and the loop continues (`GUARD_TOOL_OUTPUT_ON_BLOCK=halt` ends the run instead) | redacted result; the MCP `raw` copy (which repeats the content) is dropped | content review; approve passes the content on, deny withholds it and the run continues |
| final output | answer withheld | redacted answer | content review; approve releases the answer |

`flag` and shadow-mode decisions change nothing in the run; they are recorded.

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

On startup the agent seeds this rule for `write_file` and `delete_file` if no `guard_signal` rule
exists (`SEED_GUARD_SIGNAL_POLICIES`). This is how tool-output injection is handled in policy v3:
no off-the-shelf detector is good enough to *block* tool output (docs/EVAL.md), but its verdict is
good enough to make a human approve mutating actions after suspicious content was read. Normal
precedence still applies: explicit `block_tool` rules beat taint approvals.

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
