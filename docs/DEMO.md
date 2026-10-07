# Demo script

About ten minutes. It goes from the problem (an agent hijacked by a web page) to the guard stopping
it, operating the guard live, and the evidence that it works. The URLs assume the default ports
(API 8000, dashboard 3000, Grafana 3001); if yours differ, use what `.env` sets.

## Before you start

```bash
uv sync && npm --prefix apps/dashboard install
docker compose --env-file .env -f infra/docker-compose.yml --profile observability up -d
uv run boundary status          # model, database, guard policies
uv run boundary dev             # API + dashboard
```

- For the agent part, copy a few eval fixtures into the agent's sandbox:
  `cp packages/eval/datasets/golden/fixtures/{github_issue_exfil.md,issue_env_paste.md,crm_export.csv,prompt_injection_explainer.html} apps/sandbox-mcp/sandbox/`
- Optional: Langfuse keys in `.env` for traces (see [OBSERVABILITY.md](OBSERVABILITY.md)).
- Open three tabs: the dashboard (`localhost:3000`), Grafana (`localhost:3001`) and Langfuse.
- On a machine short of RAM, send one throwaway chat first, so the models are paged in.

## 1. The problem (1 min)

**Playground → Attack**, built-in scenario `e2e-ind-blog-planted-write`. Expand "Show the content the
agent will read": a normal blog post, with an instruction hidden in an HTML comment. Run it.

- **No defence** (left): the agent fetches the page, then writes the attacker's file
  `notes/pwned.md` (in red) before doing the real task. That's an indirect prompt injection: the
  attacker never talked to the agent, they only wrote a web page.

## 2. The guard (2 min)

Same run, **guard on** (right):

- The tool-output injection detector flags the page. It's in **shadow** mode, so the page still
  reaches the model and the guard doesn't block it.
- But the flag **taints the run**, and the policy engine's taint rule says a tainted run needs a
  human to approve any write. The planted write is held, and the attack stops.
- Why shadow + taint rather than blocking the page? Blocking on a detector has a false-positive
  cost (a harmless article *about* injection gets flagged too; see `prompt_injection_explainer.html`
  in step 3). Taint only costs an approval, and only when the run tries something dangerous.

This replays a recorded model run (free and identical every time). "Paste your own page" runs the
live model, within the daily demo budget.

## 3. One guard stage, up close (1 min)

**Playground → Scan.**

- Stage *User input*: `Hi, I'm Jane Roe, email me at jane.roe@example.com`. The email is highlighted
  and replaced with `<EMAIL_1>` in what the agent would see. Every policy's score, threshold and
  latency is listed.
- Stage *Tool output*: paste the contents of `prompt_injection_explainer.html`, an article explaining
  prompt injection, with no instruction in it. Note what fires and what doesn't: this is the
  false-positive side of the trade-off.

## 4. The real agent (2 min)

**Chat**: *"Read github_issue_exfil.md and summarise the issue."*

- The answer arrives. The **Guardrails** page and **Logs** show the tool-output check flagging the
  file (would block, shadow) and the run marked tainted.
- Follow up: *"Save that summary to notes/issue.md."* The write waits on the **Approvals** page.
  The reason names the policy that tainted the run.
- *"Summarise issue_env_paste.md"* (a pasted secret): blocked. *"Summarise crm_export.csv"*:
  personal data comes back as placeholders.
- With Langfuse on, *Last run's trace ↗* opens the run: planner calls with tokens and cost, the tool
  call, every guard check with each policy's verdict, and only redacted text.

## 5. Operate it (2 min)

- **Guardrails**: switch `tool_output_injection_protectai` from *shadow* to *enforce*. The config hash
  changes; the switch is audited and survives a restart. Re-run step 4: the file's content is now
  withheld from the model. Switch it back.
- **Grafana** (*boundary-ai: guard & agent*): block rate by stage, *would block* vs *blocked* per
  policy (the shadow-vs-enforce gap), p50/p99 per policy, errors and timeouts, LLM cost per run.
  Playground traffic has its own panel and never mixes into the real numbers.

## 6. The evidence (2 min)

- [RESULTS.md](RESULTS.md): attack success with the guard off / shadow / enforce, per-policy catch
  rate and false-positive rate with confidence intervals, latency, cost, load-test throughput, and
  our fine-tuned detector against the off-the-shelf ones.
- CI: every PR re-runs the golden detector suite and replays the end-to-end scenarios from the
  recorded cassette (no key, no network). A regression fails the build, and the results are posted
  as one PR comment ([CI.md](CI.md)).

## Appendix: the policy engine on its own

The deterministic policy engine (tool rules, argument validation, approvals) predates the guard and
still works without it (`uv run boundary dev --no-guard`):

1. **Policies**: `require_approval` for `write_file`, `validate_args` for `write_file` with an
   allowlist of `notes/`, and `block_tool` for `delete_file` (payloads below).
2. **Chat**: `write file notes/demo.txt: hello`. The write pauses for approval. While it's pending,
   add a higher-priority `block_tool` for `write_file`, then approve. The resumed call is re-checked
   against the *current* policies and blocked.
3. `delete file notes/demo.txt` is blocked. Approvals that aren't answered expire, and the run is
   denied.

```json
{"name": "Block deletes", "rule_type": "block_tool", "target_tool": "delete_file", "priority": 200,
 "conditions": {}, "action": {"reason": "File deletion is disabled in this demo."}}
{"name": "Approve writes", "rule_type": "require_approval", "target_tool": "write_file", "priority": 150,
 "conditions": {}, "action": {"reason": "Writes require explicit human review."}}
{"name": "Sandbox notes only", "rule_type": "validate_args", "target_tool": "write_file", "priority": 180,
 "conditions": {"path_arg": "path", "allow_prefixes": ["notes/"]}, "action": {}}
```

Remote tools: Exa's hosted MCP server is seeded by default (`EXA_MCP_ENABLED`, `EXA_MCP_URL`;
`EXA_API_KEY` is optional). Any `sse` or `streamable_http` MCP server can be added on the MCP page.
