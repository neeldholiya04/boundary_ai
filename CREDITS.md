# Credits and provenance

boundary-ai is one application built by our group. Most of it was written for this project; the agent
started life earlier. This file records where each part came from, so it is always clear who built
what.

## The agent and dashboard (`apps/agent/`, `apps/dashboard/`, `apps/sandbox-mcp/`)

The agent runtime, MCP tool discovery, the deterministic policy engine, the human approval workflow,
and the Next.js dashboard were first written by a member of our group as an earlier project,
[github.com/arthurW1935/boundary-ai](https://github.com/arthurW1935/boundary-ai), and brought into
this repository in Phase 2.5 as a plain copy (without its git history).
<!-- Add the author's name here. -->

Since then it has been developed as part of this project. Changes made here include:

- the guard hooks at all four stages of the agent loop, run taint, and content review (Phase 5),
- the LiteLLM planner, replacing the original OpenAI-only client, which also fixed cost tracking (Phase 2.5),
- two dependency fixes needed for it to run (Phase 2.5),
- the `guard_decisions` and `guard_overrides` tables, startup schema upgrades, and the guard API (Phases 5 and 9),
- spotlighting of tool output in the planner prompt (Phase 5),
- the Guardrails page and content reviews on the Approvals page in the dashboard (Phases 5 and 9),
- moving it from a `host/` subfolder into `apps/`, renaming its packages from `armoriq_api` /
  `armoriq_mcp` to `boundary_agent` / `boundary_mcp`, and the `boundary` CLI entrypoint (after Phase 9).

We copied it from commit `e7d279b` of the source repository. Its `api/`, `web/` and `mcp_server/`
folders map one-to-one onto our `apps/agent/`, `apps/dashboard/` and `apps/sandbox-mcp/`, so a diff against that commit (after the package rename) shows
exactly what changed here.

## Written for this project

- `packages/guard/` — the `boundary_guard` guardrails library
- `policies/` — policy set, rulesets, schemas, topic lists
- `packages/eval/` — datasets, the validator, the detector eval runner, the end-to-end harness, CI gates
- `packages/detector/` — our fine-tuned tool-output injection detector and its training pipeline
- `docs/`, CI workflows, and the integration work listed above

The hand-written attack records in the golden eval set were written by the group.

## Third-party rules

`policies/rules/providers.gitleaks.yaml` is generated from the rule catalogue of
[gitleaks](https://github.com/gitleaks/gitleaks) (MIT License, Copyright (c) 2019 Zachary Rice), pinned
to commit `b58d3f102cf3`, by `policies/rules/import_gitleaks.py`. The full licence text is reproduced
in the generated file's header. Changes on import: the generic catch-all rule, the five rules scoped to
file paths and the global allowlist are left out; a repeated capture group is replaced by the whole
match and alternatives use the first group that matched; the end-of-key context also accepts
`,` `.` `)` `]` `}` `>` so keys in prose match.

## Third-party models and datasets

Every model and dataset is pinned to a revision; licences and sources are listed in
[`packages/eval/datasets/extended/SOURCES.md`](packages/eval/datasets/extended/SOURCES.md) and the model card
[`packages/detector/MODELCARD.md`](packages/detector/MODELCARD.md). The detectors use, among others, Llama Prompt Guard 2
(Meta, gated licence), ProtectAI's `deberta-v3-base-prompt-injection-v2`, Microsoft Presidio,
`unitary/toxic-bert`, `sentence-transformers/all-MiniLM-L6-v2`, and `microsoft/deberta-v3-xsmall`
(the base of our own detector).
