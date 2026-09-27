Guardrails & safety layer

*Problem*

Tool-calling agents pull untrusted content into their context: web search results, file reads, API responses. That content can carry instructions the model then acts on. Structured policy checks on tool names and arguments stop known-bad actions, but they cannot see an injection buried in a returned web page, leaked PII in a tool response, an answer that isn't supported by what the tools returned, or a malformed output that breaks the caller downstream. Most teams ship these filters without measuring whether they work or what they cost.

*What we plan to build*

A composable input/output filtering layer that sits inline between an LLM application and everything untrusted around it, plus the evaluation harness that proves it works. We build it into a guarded MCP agent from our group's earlier project (dynamic tool discovery, deterministic policy engine, human approval workflow, admin dashboard), so the result is one application: the agent generates real traffic, and the filters sit inside its loop. The filtering layer itself stays a standalone library, so it can also wrap other LLM apps.

*Domain*

The agent is a research assistant. It fetches web pages, reads GitHub issues and READMEs, and summarises findings into notes for the user. Every tool it calls returns attacker-controllable text, so both direct and indirect injection come up naturally. All numbers are reported for this one domain.

*Key functionality and components*

- Filter layer with policies that can each be switched on or off, grouped by where they run:
  - User input: prompt-injection and jailbreak-pattern detection, PII detection and redaction, topic allow/deny.
  - Tool output: indirect prompt-injection detection, PII and secret/credential leak checks.
  - Final output: schema/JSON validation with auto-repair, a hallucination check (is the answer supported by the tool outputs actually retrieved?), toxicity, PII leak, and secret/credential leak.
- Three verdicts per policy: allow, redact/block, or escalate. An escalated (borderline) request goes to the existing human approval workflow instead of being blocked outright.
- Execution mode is explicit for each policy: blocking (inline) or async (runs after the fact, logs, alerts). Anything guarding content the model consumes right away (tool-output injection, secret leaks) must block. Slow or LLM-judged checks (hallucination, toxicity on logs) may run async. We report the latency and catch-rate trade-off between the two modes.
- Policies defined as versioned YAML. Each policy is measured on its own for catch rate, false-positive rate, added latency, and cost per request.
- Detectors combine off-the-shelf components (e.g. Prompt Guard 2 / ProtectAI DeBERTa, Presidio, gitleaks-style secret patterns, Detoxify) with at least one detector we build end-to-end. That detector is reported against the off-the-shelf baseline on a held-out split it was never tuned on.
- Evaluation data:
  - Golden set: ~50 hand-written adversarial prompts covering direct injection, jailbreaks, indirect injection via poisoned tool output, PII exposure, and secret leakage, plus benign decoys that stress false positives.
  - Extended set: a few hundred examples from public benchmarks (BIPIA, InjecAgent, AgentDojo, deepset/prompt-injections) plus benign traffic from the agent, so each policy's numbers rest on a usable sample size.
  - Train/tune and held-out test splits; every rate is reported with a 95% Wilson confidence interval.
- End-to-end agent evaluation, not just detector evaluation: attack success rate (did the agent actually carry out the injected instruction?) and benign task success rate, each measured with filters off, in shadow mode, and enforced.
- Replayable test harness: a mock MCP tool server returns fixed poisoned or clean content, so indirect-injection tests are deterministic. LLM-based detectors run at temperature 0 with cached responses in dev and CI.
- Eval-as-CI: GitHub Actions runs the golden and extended suites on every PR. A drop in catch rate or a rise in false-positive rate beyond a set tolerance fails the build. A screenshot of a blocked PR goes in the README.
- Shadow mode logs what would have been blocked before we enforce it, surfaced through the existing decision-log dashboard. The shadow-vs-enforce delta means: the share of replayed requests that would have been blocked or escalated, and the resulting change in benign task success rate.
- Observability: one Langfuse trace per request with a span per filter (verdict, score, latency, cost, policy version). Prometheus metrics feed a Grafana dashboard of block rate, escalation rate, latency and cost per policy over time.
- Load test: Locust run against the deployed service, comparing throughput and latency with filters off, blocking, and async.
- Live public URL: a playground deployed on a free tier (HF Spaces / Fly.io / Render). Visitors paste a prompt or a poisoned tool response and see each policy's verdict, score, and latency, next to the agent's response with and without filters. The demo's tools are sandboxed and read-only, with per-IP rate limits and a hard daily spend cap to prevent denial-of-wallet.
- Published numbers in the README:
  - Per policy: catch rate and false-positive rate (with confidence intervals), p50/p99 latency, cost per request.
  - Blocking vs. async overhead.
  - Attack success rate and benign task success rate with filters off / shadow / enforce.
  - Load-test throughput.
  - Built detector vs. off-the-shelf baseline.
  - Headline form: "attack success fell from X% to Y% at Z% false-positive rate, adding N ms at p99 and $C per 1k requests."

*Budget and stack*

- Total spend of $5–20. Claude Haiku / Gemini Flash is the default model and the LLM judge; classifier detectors run on CPU.
- GitHub Actions (CI), Langfuse Cloud (tracing), Grafana Cloud / Prometheus (metrics), HF Spaces or Fly.io (hosting).

*Scope boundaries and assumptions*

- The agent runtime, MCP tool discovery, deterministic policy engine, approval workflow, and admin dashboard come from our group's earlier project (see CREDITS.md). They are part of the product, and we extend them, but they are not what this project is measured on. The new work is the filtering layer and its integration into the agent, the eval sets and harness, the CI gates, observability, the public playground, and the measurements. The README and demo lead with those.
- Defence is scoped to one threat model: untrusted content entering or leaving the agent. Model weights, hosting security, and network-level attack surface are out of scope, apart from the abuse controls on the public demo.
- We aim for measured, bounded defence, not completeness. The deliverable is a reported catch rate and attack success rate with a known false-positive cost, not a claim of full coverage.
- English-language inputs only.
