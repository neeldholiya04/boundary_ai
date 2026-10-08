# Policy changelog

Every change to `guard.yaml`, a ruleset, or a schema bumps `version` in `guard.yaml`
and gets an entry here. Eval results are stamped with the config hash, so each entry
should say which numbers it is expected to move.

## v8
- `secrets` ruleset v2.1, after a second live leak: a typed `sk-proj-` key (34 lowercase letters from 8
  keys) was stored raw, written to `.env` and printed in the answer. The OpenAI rule wanted a digit, a
  capital and ≥3.0 bits/char, so a made-up or typed key passed every stage.
  - `openai_prefixed_key`: `sk-proj-` / `sk-svcacct-` / `sk-admin-` + 20 key characters match whatever the
    body looks like. Bare `sk-` keeps the old shape checks (prose and package names use it).
  - `env_credential`: `*_API_KEY` / `*_SECRET(_KEY)` / `*_TOKEN` / `*_PASSWORD` (upper case) set to any
    value, written `NAME=value` or with a quoted string. Code that reads a variable doesn't match.
  - `phrasing_with_separator`: "put this key there: …", "here's my new token: …".
  - `openai_prefixed_key` skips hyphen-joined lowercase words (`sk-proj-management-dashboard-v2`).
  - `env_credential_mapping`: the same names in code, YAML and JSON (`"OPENAI_API_KEY": "…"`, a compose
    `environment:` map); there the value must mix letters and digits, since a plain word is usually a
    constant. Public frontend names (`NEXT_PUBLIC_`, `VITE_`, …), `INVALID_`/`MAX_`/`HEADER_`-style
    constants and function calls don't match.
  - Allowlist: a body of one repeated character, whole-value references (`$VAR`, `${VAR}`, `%VAR%`,
    `{{ … }}`; anchored, so a password that starts with `$` still counts), and template values
    (`your-openai-api-key`, `change-me`, `undefined`). `generic_assignment` skips dotted code references
    (`settings.API_TOKEN`), scoped to that rule so dotted tokens (Mapbox, Google OAuth) still match.
  - `phrasing_with_separator` takes `:`/`=` only, with bounded lookaheads (a `key-key-…` run took 2.7 s).
- Two older misses the rebuilt data exposed: `google_api_key` ended in `\b`, which never matches after
  a key ending in `-` or `_`; `high_entropy_token` rejected a token followed by a full stop ("…it is
  <token>.").
- Eval: `openai_typed` and `weak_secret` fakes; 9 golden secret records (the leak at every stage) and
  5 decoys (placeholders, references, code reading env vars, "key:" prose); `openai_typed` joins the
  synthetic secrets (the second synthetic pass now shifts templates, so equal texts aren't dropped).
- Measured: `secrets` catches 100% on golden (16/16) and extended (73/73), FPR 0% on both. New e2e
  scenario `e2e-sec-typed-key` (the leak, recorded live): leaks with the guard off, stopped as shipped
  and in enforce. Test ASR guard off → as shipped: 75.0% → 12.5% (8 attack scenarios).
- Not caused by this change, surfaced by the rebuilt data: the enforced `topic` policy flags "How do I
  rotate the key stored in SECRET_KEY?" (2 extended decoys, FPR 0% → 3.8%), and ProtectAI (shadow) moves
  with the new decoys (golden FPR 52.9% → 57.9%). Topic is left as tuned; worth a look before enforcing
  it more widely.

## v7
- `secrets` runs **first** (`redact_first`): when it redacts, the stage's other policies (Presidio,
  toxicity, the NLI check, any LLM-judge rule from the dashboard) get the redacted text, so no model sees
  a raw key and none can send one to a provider. In shadow nothing is rewritten, so the others see the
  original text, as before. `secrets_egress` still blocks.
- Expected movement: `toxicity` no longer fires on key text (golden FPR 7.7% → 0%, extended 1.4% →
  1.0%: the spaced-out key it read as abuse is now `<OPENAI_KEY_1>`), and `topic` stops flagging key
  questions (extended 1.9% → 0%). `tool_output_injection_protectai` moves the other way on extended (FPR
  10.8% → 12.0%, shadow): it reads some `<OPENAI_KEY_1>` placeholders as instruction-like. Catch rates
  are unchanged everywhere. The e2e cassette gained one response (`e2e-sec-readback` under `enforce`,
  where the enforced injection detector now sees the redacted file).

## v6
- `secrets` / `secrets_egress` scan **decoded views**: base64 runs (standard or URL-safe, also after
  `NAME=`, and base64 of base64) that decode to text, and keys spelled out with spaces (`s k - p r o j
  …`). A hit redacts or blocks the encoded run (trimmed to its letters and digits, so JSON and tables
  stay intact). Work is bounded by decoded characters (256k per check), not by candidate count, so
  hundreds of UUIDs can't push a key out of reach; past the budget the check fails closed.
- **Known secrets**: the values of this deployment's own credentials (env vars matching `*_API_KEY`,
  `*_SECRET_KEY`, `*_SECRET`, `*_TOKEN`, `*_PASSWORD`) never appear in a request, tool call, tool output
  or answer, raw, URL-encoded, JSON-escaped, spaced out or base64'd. Values must be 12+ characters with
  entropy ≥ 3.5 (template defaults like `boundary-local` are skipped and logged by name), `NEXT_PUBLIC_`
  style variables are ignored, and matches are whole tokens only. Values live in memory only; the config
  hash covers the name patterns, not the values. Known secrets are never checked for the public
  playground (it would confirm guesses), and evals load the policy without them (`eval_guard`), so
  numbers don't depend on whose machine runs them.
- Eval: 3 golden evasion records and two evasion kinds in `synthetic_secrets` (210 records). On test,
  v2 without → with decoding: golden 88.2% → 100% (17 / 53), extended 92.5% → 100% (93 / 1,135), FPR 0%.
- Known limits: base64 wrapped across lines, characters separated by more than one space or by commas,
  and a decoded blob that merely *mentions* an assignment (`api_key: …`) replaces the whole blob.
- Expected movement: the evasion records are new negatives for the other policies and expose
  `toxicity` firing on character soup (the spaced-out key), so such an answer is blocked by toxicity
  rather than redacted by secrets. Tracked for the toxicity retune.

## v5
- **Secrets, after a live leak.** A user pasted a short `sk-proj-` key into chat; it was stored raw,
  written to `.env`, read back by a file search and printed in the answer, and `secrets` scored 0.0 at
  every hop. Causes: no secrets check on user input, the OpenAI rule required 40+ characters after
  `sk-proj-`, the assignment rule's `\b` never matched inside `OPENAI_API_KEY`, and the eval only held
  key shapes the ruleset already knew (100% catch on 6 positives; 0 secrets in the extended set).
- `rules/secrets.v2.yaml`: any OpenAI key shape (legacy, short project keys; body must mix digits and
  capitals), Groq, Hugging Face, xAI, GitLab, npm, SendGrid, Mailgun, Shopify and Twilio keys, Slack and
  Discord webhook URLs, `user:pass@` in any URL, `Authorization: Bearer|Token|Basic`, credentials in
  query strings, `NAME=value` with prefixed names, passwords with symbols (assigned or "the password is
  …"), natural phrasing ("my api key is …"), and two high-entropy rules for unnamed formats (32+ chars
  with upper/lower/digit, or 40–128 base64 chars with `/`/`+`; entropy ≥ 4.5; skipped when the value is
  base64 of readable text). The allowlist adds placeholders (`sk-xxxx`, `YOUR_API_KEY`, `${VAR}`,
  elided `...`), default dev passwords, lockfile `sha512-` hashes, Stripe test/publishable keys, ssh
  public keys, IPFS/Bitcoin ids, encoded image/PDF headers and PEM public-key bodies. Every rule is
  timed on 20k-char adversarial inputs in the tests (v2's first draft had a quadratic `\s*…\s*`).
- `secrets` now covers **user_input, tool_output, final_output** and **redacts** (the run carries on
  with `<OPENAI_KEY_1>`, and the user gets a `guard_notices` entry saying what was removed); new
  `secrets_egress` keeps **block** on tool_args, since arguments are never rewritten. Gates: 100% catch
  for both, FPR ≤ 5% for `secrets` and ≤ 2% for `secrets_egress` (a false alarm there blocks the task).
- Eval: 21 hand-written golden records (the incident at all four stages, other providers, decoys) and a
  generated `synthetic_secrets` source (193 records, 17 key formats). On test, v1 → v2: golden 40% →
  100% catch at 0% FPR (15 / 53); extended 58% → 100% catch at 0% FPR (88 / 1,135). The synthetic
  source was written alongside v2, so read it as a regression check, not an independent estimate.
- Known limits: random public ids that stand alone (a bare Google Docs id, a Solana address, a reCAPTCHA
  site key) are redacted too; hex-only keys with no name nearby (Datadog, GCP `private_key_id`) and a
  key hidden inside base64 of other text are not detected.
- Expected movement: the new decoys expose existing false alarms on key-related text.
  `tool_output_injection_protectai` FPR rises (golden 41.7% → 50.0%, extended 4.4% → 9.2%: it flags pages
  about keys and pages containing keys, so such pages also taint the run), and `topic` blocks 2 of 54
  extended user questions about where keys live (0% → 3.7%). Nothing else moves.

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
