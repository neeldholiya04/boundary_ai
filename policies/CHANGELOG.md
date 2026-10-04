# Policy changelog

Every change to `guard.yaml`, a ruleset, or a schema bumps `version` in `guard.yaml`
and gets an entry here. Eval results are stamped with the config hash, so each entry
should say which numbers it is expected to move.

## v4
- `topic`: exemplars moved to `topics/research.v2.yaml` / `topics/deny.v2.yaml`. Factual market and
  company lookups ("what is this company trading at", market cap, index/FX prices, earnings) were
  blocked as off-topic: they score ~0.2–0.3 against both lists and the deny side's investment-advice
  exemplar won by a few hundredths at margin 0. Added six allow exemplars for such lookups and one deny
  exemplar for "is this stock a good buy for me", so personal investment advice stays blocked.
  Expected movement: topic FPR down on finance-flavoured research; catch rate unchanged.

## v3
- Thresholds tuned on the extended dev split at a ≤2% dev FPR budget (`boundary-eval tune --max-fpr 0.02`).
- User input: **Llama Prompt Guard 2 86M** (`user_injection_promptguard`) replaces ProtectAI, at the model's
  default **0.5**. The dev-tuned 0.0116 was in the score tail and did not generalise (dev FPR 1.3% →
  extended test 3.4%, and 3 of 7 golden decoys). Decided with the group; disclosed in docs/EVAL.md.
- Tool output: `tool_output_injection_protectai` moves to **shadow** (threshold 0.9963). No off-the-shelf
  detector is fit to block tool output; its verdict becomes the run-taint trigger in Phase 5, and
  blocking waits for our own detector (Phase 8).
- `toxicity`: 0.5 → 0.119. `pii`: unchanged (identical dev results at 0.85).
- `groundedness`: NLI base model, mean over claims, **shadow** (near chance on HaluEval; the baseline
  for the LLM judge). Timeouts raised for long inputs on CPU (user input 5 s, toxicity 5 s).
- Comparison-only detectors (ProtectAI on user input, Prompt Guard 86M/22M on tool output) moved to
  `experiments/injection_comparison.yaml` so they add no production latency.

## v2
- Added off-the-shelf ML detectors, all pinned to HF Hub commit SHAs:
  - `user_injection_protectai`, `tool_output_injection_protectai` (ProtectAI DeBERTa-v3, chunked on tool output).
  - `topic` (all-MiniLM-L6-v2 nearest exemplar; `topics/research.v1.yaml` vs `topics/deny.v1.yaml`).
  - `pii` (Presidio, redact) and `pii_egress` (Presidio on tool arguments, block).
  - `toxicity` (unitary/toxic-bert, multi-label).
  - `groundedness` (cross-encoder/nli-deberta-v3-small, async, flag only).
- `jailbreak_patterns` moved to shadow mode: it is now the regex baseline next to ProtectAI.
- A commented-out `tool_output_injection_promptguard` policy is ready for when HF access is granted.
- Expected movement: large catch-rate gains on user-input injection; tool-output injection gains
  catch rate but also false alarms on security-flavoured pages (see docs/EVAL.md).

## v1
- Initial policy set: `secrets` (regex + entropy), `jailbreak_patterns` (heuristic),
  `tool_output_injection_heuristic` (shadow mode baseline), `research_note_schema`.
