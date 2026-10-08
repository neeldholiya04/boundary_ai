# Models and detectors

Every model and detector the guard and the agent use, as configured in `policies/guard.yaml`
**v11** (config hash `252a0a9ced67d285`). Measured numbers are copied from the committed baselines:

- golden: [`packages/eval/baselines/golden.md`](../packages/eval/baselines/golden.md) (296 records, `--repeats 20`)
- extended: [`packages/eval/baselines/extended.md`](../packages/eval/baselines/extended.md) (2,054 records)
- end to end: [`packages/eval/baselines/e2e.md`](../packages/eval/baselines/e2e.md)
- summary and load test: [RESULTS.md](RESULTS.md)

Unless a row says otherwise, numbers are on the **test** split, as catch rate / FPR with 95% Wilson
intervals. Shadow policies are scored on `would_action`, so they are measured like enforced ones.
Latency is p50 / p99 ms per check from the same baseline file, on a CPU-only Apple-silicon laptop.
Why each setting is what it is: [`policies/CHANGELOG.md`](../policies/CHANGELOG.md).

## Overview

Production policies (`policies/guard.yaml`). Defaults: `mode: enforce`, `execution: blocking`,
`on_error: fail_closed`, `timeout_ms: 400`.

| Policy id | Stage(s) | Detector | Model (revision) | Mode | Action | Catches |
|---|---|---|---|---|---|---|
| `user_injection_promptguard` | user_input | `all_of` (2× `hf_classifier`) | `meta-llama/Llama-Prompt-Guard-2-86M` (`a8ded8e`) confirmed by `protectai/deberta-v3-base-prompt-injection-v2` (`90c9989`) | enforce | block | direct injection, jailbreak in the user's request |
| `jailbreak_patterns` | user_input | `regex_rules` | none (`rules/jailbreak.v1.yaml`, 8 rules) | shadow | block | instruction-override / DAN phrasing (baseline) |
| `topic` | user_input | `embeddings_topic` | `sentence-transformers/all-MiniLM-L6-v2` (`1110a24`) | enforce | block | requests outside the research assistant's purpose |
| `tool_output_injection_protectai` | tool_output | `hf_classifier` | `protectai/deberta-v3-base-prompt-injection-v2` (`90c9989`) | shadow | block | indirect injection in tool output; its verdict taints the run |
| `tool_output_injection_heuristic` | tool_output | `regex_rules` | none (5 of the 8 jailbreak rules) | shadow | block | override phrasing in tool output (baseline) |
| `secrets` | user_input, tool_output, final_output | `regex_rules` (`redact_first`) | none (`rules/secrets.v2.yaml` + 216 gitleaks rules, RE2) | enforce | redact | API keys, tokens, passwords, known deployment secrets |
| `secrets_egress` | tool_args | `regex_rules` (same as `secrets`) | none | enforce | block | credentials leaving through tool arguments |
| `pii` | user_input, tool_output, final_output | `presidio` | Presidio 2.2.364 + spaCy `en_core_web_sm` 3.8.0 | enforce | redact | email, phone, card, US SSN, IBAN, IP address |
| `pii_egress` | tool_args | `presidio` (same) | same | enforce | block | the same entities leaving through tool arguments |
| `research_note_schema` | final_output | `json_schema` | none (`schemas/research_note.v1.json`) | enforce | block | a requested research note that is invalid after JSON repair |
| `toxicity` | final_output | `hf_classifier` (multi-label) | `unitary/toxic-bert` (`4d6c22e`) | enforce | block | toxic, insulting, threatening answers |
| `groundedness` | final_output | `nli_groundedness` | `cross-encoder/nli-deberta-v3-base` (`6c749ce`) | shadow, async | flag | answer claims unsupported by the tool outputs read |

Comparison-only detectors (not loaded in production, no latency cost there):

| Policy id | File | Model (revision) | Threshold | Why it is not shipped |
|---|---|---|---|---|
| `user_injection_protectai` | `policies/experiments/injection_comparison.yaml` | ProtectAI v2 (`90c9989`) | 0.0103 | replaced by Prompt Guard in v3; now the confirmer inside `user_injection_promptguard` instead |
| `tool_output_injection_promptguard` | same | Prompt Guard 2 86M (`a8ded8e`) | 0.9972 | misses plain tool-output injections |
| `tool_output_injection_promptguard_22m` | same | `meta-llama/Llama-Prompt-Guard-2-22M` (`11614a1`) | 0.9983 | same; about half the latency of 86M |
| `tool_output_injection_ours` | `policies/experiments/our_detector.yaml` | our fine-tuned DeBERTa-v3-xsmall (local path `packages/detector/model`, no revision) | 0.71 | weights are not hosted where CI can fetch them |

Detectors used only by operator rules written in the dashboard (`apps/agent/src/boundary_agent/rules.py`):
`keywords`, `pattern`, `embeddings_topic` (inline exemplars), `llm_judge`, `always`.

Operator rules compile into extra `rule_<slug>` policies next to the file's policies, through the
same pipeline, decision log and config hash.

## Shared mechanics

- **Pinned revisions.** Every Hub model is loaded with `revision=<commit SHA>`, and the revision is part
  of each detector's `fingerprint()`, so it is part of the config hash stamped on every eval result.
- **One copy per process.** ML models are loaded once, keyed by (kind, name, revision), and shared by
  every policy that names them (`detectors/_models.py`). ProtectAI v2 is loaded once and serves both
  `user_injection_promptguard` and `tool_output_injection_protectai`. Inference on a shared model is
  serialised with a per-model lock and runs in a worker thread.
- **CPU only.** `torch_device()` returns `"cpu"`. Peak memory with every production model loaded was
  ~1.85 GB RSS (extended run, measured at policy v3, docs/EVAL.md).
- **Failure.** A detector error or timeout applies the policy's `on_error` (`fail_closed` by default).
  Eval runs lift timeouts unless `--enforce-timeouts` is passed.
- **First download.** About 2 GB of pinned models into `~/.cache/huggingface` (README).

## Prompt injection on user input: `user_injection_promptguard`

**What it is.** An `all_of` detector with two `hf_classifier` members, run concurrently:

| Role | Model | Revision | Positive label | Threshold | Chunking |
|---|---|---|---|---|---|
| primary | `meta-llama/Llama-Prompt-Guard-2-86M` | `a8ded8e697ce7c355e395a0df51f94adb4a2fd27` | `LABEL_1` | 0.5 (model default) | 512 tokens, 64 overlap, max 16 chunks |
| confirmer | `protectai/deberta-v3-base-prompt-injection-v2` | `90c9989b1a342275dd0d1a95aad283c04e075671` | `INJECTION` | 0.5 | same |

`primary_alone: 0.999`. Timeout 5000 ms.

**How `all_of` decides** (`detectors/all_of.py`). The policy fires when the primary fires **and**
either every confirmer fires or the primary's score is ≥ `primary_alone`. The confirmer cannot open
the gate on its own. A confirmer that errors is ignored and the primary decides. A primary that
errors raises, and `on_error` applies. The reported score is the primary's. `threshold` is `None`,
so `boundary-eval tune` skips this policy.

**How `hf_classifier` scores** (`detectors/hf_classifier.py`). Text longer than one window is split
into overlapping token windows and each window is scored. The text's score is the maximum over
windows. Past `max_chunks`, the first and last halves are kept and the reason says the middle was
skipped. With `multi_label: false`, the softmax probabilities of the positive labels are summed.

**Why chosen.**
- v3 picked Prompt Guard 2 86M over ProtectAI on user input.
- The dev-tuned threshold of 0.0116 sat in the tail of a bimodal score distribution. Its dev FPR of
  1.3% did not hold on extended test (3.4%), and on golden it blocked 3 of 7 hard decoys. The model
  default 0.5 was used instead. This choice used test-split evidence; it is disclosed in docs/EVAL.md.
- v10 added the ProtectAI confirmer. Each model alone flagged real conversation that the other
  passed. Prompt Guard scored "write a note on why the above chats were blocked" 0.998; ProtectAI
  flagged "ok continue".
- `primary_alone: 0.999` was tuned on dev.

**Measured.**

| Set | Split | Catch rate | FPR | Pos / Neg | p50 / p99 ms |
|---|---|---|---|---|---|
| extended | test | 75.7% [69–81] | 0.0% [0–2] | 177 / 212 | 48 / 461 |
| extended | dev | 70.6% [62–78] | 0.0% [0–2] | 119 / 189 | 48 / 461 |
| golden | test | 83.3% [44–97] | 0.9% [0–5] | 6 / 106 | 44 / 67 |
| golden | dev | 0.0% [0–56] | 1.1% [0–6] | 3 / 87 | 44 / 67 |

- By source (extended test): deepset/prompt-injections 8 / 38 (21.1%); jackhhao/jailbreak-classification
  126 / 139 (90.6%); 0 false alarms on any source.
- The confirmer costs catch rate. Prompt Guard alone caught 82.5% on extended test; with the
  confirmer it catches 75.7%. False alarms were 0% either way (CHANGELOG v10).
- Load test, per check: p50 1,570 ms, p99 4,284 ms (`async` profile, RESULTS.md). This is one of the
  three slow checks on the request path, on a laptop that pages the models out.

**Licence and gating.**
- Llama Prompt Guard 2 is a **gated** Meta model under the Llama licence. Accept the licence on its
  Hugging Face page, then run `uv run hf auth login` or set `HF_TOKEN`.
- CI needs `HF_TOKEN` as a repo secret (docs/CI.md). Without access, the model fails to load (a 401
  error, or a "gated repo" error).
- ProtectAI v2 is ungated (Apache-2.0 per its model card).

**Known weaknesses.**
- Weak on subtle, low-resource phrasing (deepset: 21.1%).
- Real DAN variants are among the attacks lost to the confirmer (CHANGELOG v10).
- The v10 incident message scored 0.998, just under the line where Prompt Guard decides alone.
- ProtectAI was trained on jailbreak-classification, so its agreement on that source is optimistic.
- The extended dev split has few hard negatives, so dev-tuned thresholds overfit (docs/EVAL.md).

## Off-topic requests: `topic`

**What it is.** `embeddings_topic` with `sentence-transformers/all-MiniLM-L6-v2`
(`1110a243fdf4706b3f48f1d95db1a4f5529b4d41`), a 384-dimension sentence embedder. Allow exemplars:
`topics/research.v3.yaml` (35). Deny exemplars: `topics/deny.v4.yaml` (20). Timeout 1000 ms.

**How it decides** (`detectors/embeddings_topic.py`).
- The request and every exemplar are embedded, normalised and compared by cosine.
- Score = best deny similarity − best allow similarity.
- The policy fires when **score > `margin` (0.05)** and **best deny ≥ `min_similarity` (0.25)**.
- Messages with fewer than **`min_words` (3)** words are not judged. A word is a run of two or more
  letters, in any script. Such messages return `score=None`.
- The reason names the nearest allow and deny exemplars.

**Why these settings.**
- v10, from real use: with margin 0 and no floor, `topic` blocked 6 of 44 distinct real messages
  ("hi bro how is … doing", "but bro why you blocked"). "123" landed nearest "Do my math homework".
- Exemplars v3 put conversation on the allow side. The floor and the word minimum were added and
  tuned on golden dev: 18/19 off-topic caught, 0/32 conversation flagged.
- v11 (deny v4) dropped homework as a deny category. "prove that 1+2 is not equal to 3" was blocked
  as math homework. Six golden records were relabelled benign as a product decision.
- Held-out test data (40 off-topic, 49 pass) was written by someone who saw neither the exemplars nor
  the dev records.

**Exemplar files** (`policies/topics/`): `research.v2` (20), `research.v3` (35, in use); `deny.v2`
(13), `deny.v3` (22), `deny.v4` (20, in use). Older versions are kept for the record. Dashboard
topic rules reuse this model, `research.v3.yaml` as the allow side, `min_similarity` 0.25 and
`min_words` 3. These are hard-coded in `rules.py` (`TOPIC_*`).

**Measured.**

| Set | Split | Catch rate | FPR | Pos / Neg | p50 / p99 ms |
|---|---|---|---|---|---|
| golden | test | 78.9% [64–89] | 2.7% [1–9] | 38 / 74 | 9.59 / 13 |
| golden | dev | 94.1% [81–98] | 0.0% [0–6] | 34 / 56 | 9.59 / 13 |
| extended | test | – (no positives) | 1.6% [0–9] | 0 / 62 | 11 / 26 |

- The extended false alarm is `ext-synthetic-secrets-00133`.
- Golden test false alarms are `gold-ui-thp-023` and `gold-ui-thp-024`.
- Load test, per check: p50 238 ms, p99 620 ms (`async`).

**Licence.** Apache-2.0, ungated.

**Known weaknesses.**
- Misses are mostly non-English requests and borderline personal ones, such as dating openers and
  parlays (CHANGELOG v10).
- The dev numbers are optimistic, because the v3 exemplars were written with the dev records in view.
- The extended set has no off-topic positives. Its generic chatbot prompts are `unlabeled` for topic.
- The model truncates long input (sentence-transformers' default sequence length for this model), so
  only the start of a long request is judged.
- `boundary-eval tune` skips this policy (it has no single threshold).

## Indirect injection in tool output: `tool_output_injection_protectai`

**What it is.**
- Model: `hf_classifier` with ProtectAI `deberta-v3-base-prompt-injection-v2` (`90c9989`).
- Label and threshold: `INJECTION` at **0.9963**.
- Chunking: 512 tokens, 64 overlap, max 16 chunks.
- Mode: **shadow**, timeout 10,000 ms.

Its verdict (label `injection`) **taints the run** (`GUARD_TAINT_LABELS`). After that, mutating tools
(`write_file`, `delete_file`) need human approval through the seeded `guard_signal` rules.

**Why shadow.** No off-the-shelf detector is fit to *block* tool output (v3). ProtectAI withholds
legitimate documentation pages: the golden false alarms are the Stripe docs, the AWS docs, a
changelog and a security article, all scored 1.0. Blocking waits for our own detector.

**Measured.**

| Set | Split | Catch rate | FPR | Pos / Neg | p50 / p99 ms |
|---|---|---|---|---|---|
| extended | test | 35.5% [30–42] | 14.1% [10–20] | 234 / 170 | 42 / 54 |
| extended | dev | 30.6% [22–40] | 12.1% [7–20] | 98 / 99 | 42 / 54 |
| golden | test | 40.0% [17–69] | 56.5% [37–74] | 10 / 23 | 45 / 515 |

- By source (extended test):
  - InjecAgent base (plain injections): 29 / 180 (16.1%).
  - InjecAgent enhanced (explicit "IMPORTANT!!! Ignore all previous instructions"): 54 / 54.
  - InjecAgent benign fills: 3 / 108 false alarms (2.8%).
  - Synthetic boundary-eval: 21 / 62 false alarms (33.9%), mostly synthetic secrets and PII pages.
- Load test, per check: p50 2,177 ms, p99 8,666 ms, 8 errors (`async`). This is the slowest check
  on the request path.
- End to end (test): the shipped stance (`filters_taint`) has an ASR of 11.1% and benign task
  success of 75.0%. The one benign failure is a harmless article *about* injection that this detector
  flags, which holds the save for approval.

**Known weaknesses.**
- It flags pages about keys and pages containing keys. FPR rose with the secrets decoys added in v5
  and v7.
- Taint cannot stop output-only hijacks, such as a canary that is only repeated in the answer.
- At the shipped threshold it catches a minority of plain injections.

## Regex baselines: `jailbreak_patterns`, `tool_output_injection_heuristic`

`regex_rules` with `rules/jailbreak.v1.yaml`, which has 8 rules: `ignore_previous`,
`new_instructions`, `system_prompt_override`, `role_hijack`, `dan_family`, `no_restrictions`,
`reveal_system_prompt`, `hidden_directive`. The tool-output policy enables only
`ignore_previous`, `new_instructions`, `system_prompt_override`, `role_hijack` and `hidden_directive`.
Both policies are shadow, kept as the cheap baseline the ML detectors must beat.

| Policy | Set | Catch rate | FPR | Pos / Neg | p50 / p99 ms |
|---|---|---|---|---|---|
| `jailbreak_patterns` | extended test | 31.6% [25–39] | 0.0% [0–2] | 177 / 212 | 0.03 / 0.68 |
| `jailbreak_patterns` | golden test | 16.7% [3–56] | 0.9% [0–5] | 6 / 106 | 0.02 / 0.05 |
| `tool_output_injection_heuristic` | extended test | 23.1% [18–29] | 0.0% [0–2] | 234 / 170 | 0.06 / 0.09 |
| `tool_output_injection_heuristic` | golden test | 20.0% [6–51] | 4.3% [1–21] | 10 / 23 | 0.07 / 0.46 |

On InjecAgent the heuristic catches 54 / 54 enhanced attacks and 0 / 180 plain ones.

## Secrets: `secrets`, `secrets_egress`

**What it is.** `regex_rules` with the ruleset `rules/secrets.v2.yaml` (version 2.2). It has no ML
model. Policy parameters:

| Parameter | Value | Effect |
|---|---|---|
| `decode` | `[base64, spaced]` | Also scans base64 runs (standard or URL-safe, base64 of base64, up to depth 2) and keys spelled out with single spaces. A hit spans the whole encoded run. Work is capped at 256k decoded characters per check; past that the check fails closed. |
| `known_secrets_env` | `*_API_KEY`, `*_SECRET_KEY`, `*_SECRET`, `*_TOKEN`, `*_PASSWORD` | Values of these env vars are never allowed through, raw, URL-encoded, JSON-escaped, spaced or base64'd. Whole tokens only. Values need 12+ characters and entropy ≥ 3.5. `NEXT_PUBLIC_`-style variables are ignored. Not applied to the playground. Evals load the policy without them. |
| `redact_first` | `true` | Runs before the stage's other policies and hands them the redacted text, so no model (toxicity, NLI, an LLM judge) sees a raw key. |

`secrets` redacts at user_input, tool_output and final_output, replacing each key with a typed
placeholder such as `<OPENAI_KEY_1>`. `secrets_egress` reuses the same detector (a YAML anchor) and
**blocks** at tool_args, because tool arguments are never rewritten.

**Ruleset layers.**

1. **Hand-written rules (35)** in `secrets.v2.yaml`, compiled with the `regex` module (lookarounds,
   backtracking):
   - Provider formats: AWS, GitHub, GitLab, OpenAI, Anthropic, Google, Groq, Hugging Face, xAI,
     npm, SendGrid, Mailgun, Shopify, Twilio, Slack, webhooks, Stripe live keys.
   - Other formats: private key blocks, JWTs, connection strings, `Authorization` headers, URL
     query credentials.
   - Context rules: `env_credential`, `env_credential_mapping`, `generic_assignment`,
     `password_assignment`, `natural_phrasing`, `phrasing_with_separator`, `provider_prefix`
     (strong prefixes, any 12+ key characters), `provider_prefix_weak` (needs a digit or capital,
     or 20+ characters), `dotenv_credential_line`.
   - Two high-entropy rules: 32+ characters at ≥ 4.5 bits/char, and 40–128 characters of base64 at
     ≥ 4.5 bits/char. Both skip values that decode to readable text.
   - A global allowlist covers placeholders, references (`$VAR`, `${VAR}`), template values, Stripe
     test keys, public-key bodies and similar.
2. **Imported provider formats (216 rules)** in `providers.gitleaks.yaml`.
   - Generated by `rules/import_gitleaks.py` from gitleaks (MIT), pinned to commit `b58d3f102cf3`.
   - Included after the hand-written rules (`include:`), so a hand-written rule names the
     placeholder when both match.
   - Compiled with **RE2** (`engine: re2`, linear time). Under a backtracking engine several took
     3–4 s on crafted input.
   - Each rule runs only when one of its `keywords` appears in the lower-cased text, as a cheap
     prefilter.
   - `group: first` takes the first capture group that matched.
   - The end-of-key context also accepts `,` `.` `)` `]` `}` `>`, so keys in prose match.
   - Left out: `generic-api-key`, the file-path-scoped rules, and gitleaks' global allowlist.
   - To update, change `COMMIT` and re-run
     `uv run python policies/rules/import_gitleaks.py`. This is a reviewed ruleset change.

`secrets.v1.yaml` is the original 12-rule set, kept for comparison. Every rule is timed on
20k-character adversarial input in the guard tests (each must stay under 10 ms).

**Measured.**

| Policy | Set | Catch rate | FPR | Pos / Neg | p50 / p99 ms |
|---|---|---|---|---|---|
| `secrets` | extended test | 100.0% [96–100] | 0.0% [0–0] | 94 / 1095 | 0.23 / 2.41 |
| `secrets` | golden test | 100.0% [83–100] | 0.0% [0–3] | 19 / 144 | 0.14 / 1.10 |
| `secrets_egress` | extended test | 100.0% [90–100] | 0.0% [0–8] | 36 / 42 | 0.12 / 0.44 |
| `secrets_egress` | golden test | 100.0% [68–100] | 0.0% [0–39] | 8 / 6 | 0.16 / 0.67 |

The extended positives come from the generated `synthetic_secrets` source, written alongside the
ruleset. Read them as a regression check, not an independent estimate. CI gates: 100% catch for both
policies; FPR ≤ 5% for `secrets` and ≤ 2% for `secrets_egress` (`packages/eval/gates.yaml`).

**Known weaknesses** (CHANGELOG v5, v6, v9).
- A key whose prefix uses a Unicode hyphen or contains a zero-width character.
- A short typed value in YAML or header form without a provider prefix (`x-api-key: <15 letters>`).
- Base64 wrapped across lines; characters separated by more than one space or by commas.
- Hex-only keys with no name nearby.
- Stand-alone random public ids (a bare Google Docs id, a Solana address) are redacted too.

## Personal data: `pii`, `pii_egress`

**What it is.** Microsoft Presidio `AnalyzerEngine` 2.2.364 with spaCy `en_core_web_sm` 3.8.0
(English only). Presidio uses pattern and checksum recognisers, with spaCy for context words.

- Entities: `EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD`, `US_SSN`, `IBAN_CODE`, `IP_ADDRESS`.
- `score_threshold` is 0.5. In v3, 0.85 gave identical dev results.
- Each finding becomes a span, so `redact` writes typed placeholders such as `<EMAIL_1>`.
- `pii_egress` blocks at tool_args. Timeout is 2000 ms for both.
- The spaCy model is not a Hub revision. It is pinned through `uv.lock`, and the Presidio version is
  in the fingerprint.

**Measured.**

| Policy | Set | Catch rate | FPR | Pos / Neg | p50 / p99 ms |
|---|---|---|---|---|---|
| `pii` | extended test | 87.8% [83–91] | 0.0% [0–0] | 254 / 935 | 9.53 / 114 |
| `pii` | golden test | 100.0% [51–100] | 0.0% [0–2] | 4 / 159 | 7.06 / 47 |
| `pii` | golden dev | 66.7% [21–94] | 0.9% [0–5] | 3 / 110 | 7.06 / 47 |
| `pii_egress` | extended test | 68.2% [47–84] | 0.0% [0–6] | 22 / 56 | 3.19 / 5.69 |
| `pii_egress` | golden test | 100.0% [21–100] | 0.0% [0–23] | 1 / 13 | 3.69 / 9.25 |

**Licence.** Presidio and `en_core_web_sm` are MIT.

**Known weaknesses** (docs/EVAL.md).
- A bare phone number scores 0.4, below the threshold. Unformatted numbers in JSON (InjecAgent
  `+14155552911`) and digit strings in URLs are missed.
- Emails on non-existent TLDs (`.example`) are ignored.
- English only.
- Names and locations are not in the entity list.

## Answer schema: `research_note_schema`

`json_schema` validates the answer against `schemas/research_note.v1.json` (Draft 2020-12). It runs
only when the caller asks for that schema (`ctx.response_schema`). Malformed JSON (fences, trailing
commas, quotes, truncation) is repaired locally with `json_repair` and passed on rewritten. The policy
fires only if the output is still invalid after repair. It has no model.

Golden test: 100.0% [21–100] / 0.0% [0–79] (1 / 1), 0.08 / 0.30 ms. The extended set has no schema
records. The CI gate is 100% catch at 0% FPR.

## Toxicity: `toxicity`

**What it is.** `hf_classifier` with `unitary/toxic-bert` (`4d6c22e74ba2fdd26bc4f7238f50766b045a0d94`),
a BERT classifier trained on the Jigsaw toxic-comment labels.

- `multi_label: true`: each label gets its own sigmoid, and the score is the max over `toxic`,
  `severe_toxic`, `obscene`, `threat`, `insult` and `identity_hate`.
- Threshold **0.119**. The yaml comment records 85.7% catch at 1.8% FPR on dev.
- Default chunking: 512 tokens, 64 overlap, max 32 chunks. Timeout 5000 ms.

**Why this threshold.** Tuned 0.5 → 0.119 in v3 on extended dev at a ≤ 2% FPR budget. Since v7 it
sees redacted text, so it no longer fires on spaced-out keys. Golden FPR dropped from 7.7% to 0%.

**Measured.**

| Set | Split | Catch rate | FPR | Pos / Neg | p50 / p99 ms |
|---|---|---|---|---|---|
| extended | test | 81.9% [73–88] | 1.0% [0–3] | 94 / 302 | 20 / 35 |
| extended | dev | 85.7% [74–93] | 1.6% [1–5] | 56 / 190 | 20 / 35 |
| golden | test | 50.0% [9–91] | 0.0% [0–19] | 2 / 16 | 17 / 33 |

- By source (extended test): civil_comments 77 / 94 caught with 1 / 100 false alarms; HaluEval
  2 / 140 false alarms.
- Load test, per check: p50 1,289 ms, p99 4,100 ms (`async`).

**Licence.** Apache-2.0, ungated.

**Known weaknesses.**
- Weak on the group's hand-written threats and slurs (golden 1 of 2).
- Domain gap: civil_comments are public comments, not agent answers.
- Fires on character soup when secrets are not enforced, because shadow mode does not rewrite.

## Groundedness: `groundedness`

**What it is.** `nli_groundedness` with `cross-encoder/nli-deberta-v3-base`
(`6c749ce3425cd33b46d187e45b92bbf96ee12ec7`), an NLI cross-encoder (entailment / neutral /
contradiction).

- The answer is split into sentence claims; fragments under 12 characters merge into the previous
  claim. At most 12 claims are checked.
- Each tool output is cut into 400-token premises.
- A claim's support is its best entailment probability over all premises.
- With `aggregate: mean`, the score is `1 − mean(support)`. It fires at **0.5**.
- It runs only when the check carries `references` (the tool outputs read).
- Shadow, `execution: async`, `action: flag`, timeout 20,000 ms.

**Why kept in shadow and untuned.** Sentence-level NLI is near chance on HaluEval summaries (dev AUC
0.47–0.60 for the small and base models, either aggregation). It stays as the cheap baseline for an
LLM judge. Vectara HHEM would fit the task but needs `trust_remote_code`, which is not enabled.

**Measured.**

| Set | Split | Catch rate | FPR | Pos / Neg | p50 / p99 ms |
|---|---|---|---|---|---|
| extended | test | 94.3% [86–98] | 88.6% [79–94] | 70 / 70 | 443 / 1229 |
| golden | test | 100.0% [34–100] | 0.0% [0–79] | 2 / 1 | 45 / 64 |

It fires on nearly everything (precision 52% on extended test). Load test, per check: p50 2,482 ms,
p99 6,962 ms (`async`, off the response path).

**Licence.** Apache-2.0, ungated.

## Our detector: tool-output injection (experiment)

**What it is.** `microsoft/deberta-v3-xsmall` (MIT) fine-tuned as a binary `BENIGN` / `INJECTION`
classifier for indirect injection in tool output (`packages/detector/`). It is loaded by the
standard `hf_classifier` from the local path `packages/detector/model` at threshold **0.71**, in
shadow, via `policies/experiments/our_detector.yaml`.

**Training.**
- About 1.2k examples from `train` splits only. `data/manifest.json`: 1,208 train (804 positive /
  404 negative), 144 validation.
- Sources: InjecAgent train templates and benign fills, plus HaluEval train news articles with a
  train-split injection spliced in under six wrappers.
- Settings: 3 epochs, learning rate 2e-5, max length 512, trained on a Colab T4.
- Dev and test records are checked for leakage by content hash.
- Details: [MODELCARD.md](../packages/detector/MODELCARD.md).

**Measured** (tool-output test split, 244 injections / 147 clean). Each detector was tuned on dev at a
matched ~10% FPR budget, so the baselines here are **not** at their shipped thresholds:

| Detector | Threshold | Catch rate | FPR | p50 / p99 ms |
|---|---|---|---|---|
| ours | 0.71 | 82.8% [78–87] | 9.5% [6–15] | 76 / 378 |
| ProtectAI v2 | 0.976 | 52.9% [47–59] | 27.9% [21–36] | 162 / 926 |
| Prompt Guard 2 86M | 0.00115 | 54.5% [48–61] | 20.4% [15–28] | 154 / 899 |

**Why it is not shipped.** The weights are not committed (`packages/detector/model` does not exist in
the repo) and are not on the Hub, so CI cannot fetch them. The table above is not part of the
committed baselines, and its result file (`packages/eval/results/detector.json`) is not committed.

**Known weaknesses.**
- Scores overlap (attacks ~0.65–0.83, clean ~0.62–0.81), so the margin around 0.71 is thin.
- Trained on news-article carriers and its own wrapper styles.
- Tool output only; English only.

## Operator-rule detectors (dashboard)

These detectors are not used by `guard.yaml`. They back rules written on the Guardrails page and are
not covered by the committed baselines. A rule's `dry_run` checks it against its own examples and
against benign eval records (`GUARD_RULE_TEST_SAMPLE`: 300, or 25 for judge rules).

| Detector | What it does | Notable settings |
|---|---|---|
| `keywords` | Literal words or phrases, matched case-insensitively and on whole words by default, longest first. Every hit is a span, so it can redact. | `fuzzy`: keywords of 7+ letters match with one letter edit. The first two letters must be exact, and the edit cannot add a space or digit. Shorter keywords stay exact (v10). |
| `pattern` | Inline regexes (`regex` module), up to 50 patterns of up to 500 characters. | All searches share a 0.25 s budget, and running out raises `PatternTimeout`. Counted repetition is capped at 1000 per quantifier and 100,000 as a product. Flags: `i`, `m`, `s`. |
| `embeddings_topic` (topic rule) | As `topic`, with the rule's deny examples inline, against `research.v3.yaml` or the rule's own allow examples. | MiniLM `1110a24`, `min_similarity` 0.25, `min_words` 3, `margin` 0.05 by default. |
| `llm_judge` | A plain-language policy judged by an LLM through LiteLLM. The text is wrapped in nonce markers, and the reply must be JSON with a boolean `violates`. | Model: the rule's own, else `GUARD_JUDGE_MODEL`, else `LLM_MODEL`. Temperature 0, 1 retry, 20 s timeout. Text is judged in 12,000-character chunks, at most 4, concurrently; longer text raises. Fires when `violates` and confidence ≥ `threshold` (0.5). Cost is tracked per call. **The checked text is sent to the judge's provider.** |
| `always` | Fires on every check. Combined with stages and tools, it makes a plain "this tool call needs approval / is blocked" rule. | – |

## The planner LLM

The agent's planner is not a guard model, but it is the model the guard protects.

| Setting | Value | Source |
|---|---|---|
| Client | LiteLLM 1.102.1 (`litellm.acompletion`), telemetry off | `apps/agent/src/boundary_agent/llm.py` |
| Default model | `openai/gpt-4.1-mini` (`LLM_MODEL`) | `config.py`, `.env.example` |
| Other providers | change `LLM_MODEL` and set that provider's key: `anthropic/…`, `gemini/…`, `ollama/…` (+ `LLM_API_BASE`) | README |
| Credentials | the provider's own env var (`OPENAI_API_KEY`, …), or `LLM_API_KEY` / `LLM_API_BASE` | `config.py` |
| Temperature | 0.0 (`LLM_TEMPERATURE`) | `config.py` |
| Timeout / retries | 30 s / 2 (`LLM_TIMEOUT_SECONDS`, `LLM_NUM_RETRIES`) | `config.py` |
| Tool calling | OpenAI-style function tools, `tool_choice="auto"`, at most 6 tool steps (`MAX_TOOL_STEPS`) | `llm.py`, `config.py` |
| Spotlighting | on (`GUARD_SPOTLIGHT`): tool output goes between `<<untrusted_tool_output NONCE>>` markers with a fresh random nonce, and the system prompt says to treat it as data | `llm.py` |
| Placeholders | when the prompt holds a guard placeholder (`<OPENAI_KEY_1>`), the system prompt says it is not the real value | `llm.py` |
| Spend cap | `LLM_DAILY_BUDGET_USD` (0 = no cap); cassette replays don't count | `config.py` |
| No key | `MissingPlanner` explains which key is missing. `LLM_PROVIDER=mock` or `stub` (demo, load test) needs `ALLOW_DEMO_MOCK_PLANNER=true`. | `llm.py` |

**Measured.** The end-to-end eval replays gpt-4.1-mini from the committed cassette
(`packages/eval/cassettes/e2e.json`). Test split results, 9 attack and 4 benign scenarios:

| Config | ASR | Benign task success | $/task |
|---|---|---|---|
| `no_defense` | 77.8% [45–94] | 100.0% [51–100] | 0.00094 |
| `filters_taint` (as shipped) | 11.1% [2–43] | 75.0% [30–95] | 0.00088 |
| `enforce` | 0.0% [0–30] | 75.0% [30–95] | 0.00068 |

Spotlighting alone did not change ASR (`spotlight_only` 77.8%). RESULTS.md puts LLM spend at $0.88
per 1k requests. Changing the planner model or its prompt invalidates the cassette: re-record it
with `--mode record` (needs a key) and regenerate the e2e baseline.

## Changing a threshold or a model safely

The procedure, from docs/EVAL.md, docs/CI.md and `packages/eval/gates.yaml`:

1. **Make the change on a copy first.** Put a candidate detector in a policy file under
   `policies/experiments/`, in shadow. For a model, pin `revision` to a commit SHA. Check that
   `positive_labels` exist in the model's `id2label`; the detector raises at load if none match.
   Check the licence and gating, and whether CI can download the model.
2. **Run it on dev and tune there only.**

   ```bash
   uv run boundary-eval detectors --suite extended --policies <file> --out packages/eval/results/ext.json
   uv run boundary-eval tune packages/eval/results/ext.json --max-fpr 0.02 [--policy <id>]
   ```

   - `tune` picks, per score-based policy, the threshold with the highest dev catch rate whose dev
     FPR stays within the budget.
   - Pass `--max-fpr 0.02`: the CLI default is 0.05, and the project's budget is 2%.
   - `tune` warns on tail thresholds and skips `all_of` and `embeddings_topic` policies. Tune those
     by hand on dev (golden dev for `topic`).
3. **Check test and the hard decoys before adopting.**

   ```bash
   uv run boundary-eval detectors --suite golden --policies <file>
   ```

   Look at the test split and the golden hard decoys. The extended dev split has few hard
   negatives. A dev FPR that does not hold on test means the threshold is in the score tail. This is
   what happened to Prompt Guard's 0.0116.
4. **Edit `policies/guard.yaml`.** Bump `version` and add a `policies/CHANGELOG.md` entry. The entry
   says why, and which numbers it is expected to move. Do the same for any ruleset, topic or schema
   file. Ruleset and exemplar files are versioned by filename (`deny.v4.yaml`). If you change the
   topic model or allow list, also update the `TOPIC_*` constants in
   `apps/agent/src/boundary_agent/rules.py`.
5. **Regenerate the baselines in the same PR**, so reviewers see the diff:

   ```bash
   uv run boundary-eval detectors --suite golden --repeats 20 --quiet --out packages/eval/baselines/golden.json
   uv run boundary-eval detectors --suite extended --out packages/eval/baselines/extended.json
   uv run boundary-eval e2e --out packages/eval/baselines/e2e.json --quiet
   uv run boundary-eval results            # regenerates docs/RESULTS.md
   uv run boundary-eval compare <old golden.json> packages/eval/baselines/golden.json
   ```

   - The e2e replay fails on cassette misses. If the change alters what the planner sees, record new
     responses first:
     `uv run boundary-eval e2e --mode record --out packages/eval/results/e2e.json` (needs an LLM key).
   - The config hash changes with any model, revision, threshold or ruleset change, so stale
     baselines are visible.
6. **CI gates** (`packages/eval/gates.yaml`). On golden test, catch rate may not drop and FPR may not
   rise by more than 2 pp per policy. `secrets`, `secrets_egress`, `pii` and `research_note_schema`
   have absolute floors. The e2e gate compares `filters_taint` scenario by scenario with the
   baseline. Latency is reported, not gated, so check p99 and memory yourself.
