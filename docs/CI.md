# CI and eval gates

Every push and PR runs [`.github/workflows/ci.yml`](../.github/workflows/ci.yml): lint, tests, the
dataset validator, the **detector eval gate**, the **end-to-end gate** (cassette replay), and
the dashboard build. A nightly job refreshes the replay cassette against a live model.

## The `test` job, in order

1. **Lint** — `ruff check` + `ruff format --check`.
2. **Test** — `pytest`. Stays offline: the ML-detector tests skip when the models are not in the
   cache, and the end-to-end tests use a tiny regex policy, so no model downloads and no key needed.
3. **Validate datasets** — schema, duplicate ids, split leakage, placeholders.
4. **Detector eval gate** — `boundary-eval detectors --suite golden` then `compare` against
   `packages/eval/baselines/golden.json` using [`packages/eval/gates.yaml`](../packages/eval/gates.yaml). This step downloads the
   pinned models (cached across runs), including the gated Prompt Guard, so it needs `HF_TOKEN`.
5. **End-to-end gate** — `boundary-eval e2e --mode replay --gates packages/eval/gates.yaml` replays the
   agent runs from the committed cassette (no LLM key, no network) and compares the `filters_taint`
   config, scenario by scenario, with `packages/eval/baselines/e2e.json`. It fails if an attack that was
   stopped in the baseline now succeeds, or a benign task that passed now fails, and names the
   scenario. Improvements and new scenarios are reported only. A PR that changes the numbers on purpose
   regenerates the baseline (`boundary-eval e2e --out packages/eval/baselines/e2e.json`).
6. **Comment / summary** — the detector and e2e tables are written to the job summary and posted as a
   single **sticky PR comment** (updated in place, not re-posted).
7. **Enforce gates** — the job fails if either gate failed. The comment is posted *before* this, so a
   blocked PR always shows why.

The gates run but don't fail the job until the last step, so the PR comment is always published.

## Gates (`packages/eval/gates.yaml`)

- **Detector, relative:** on the test split, catch rate may not drop, and FPR may not rise, by more
  than 2pp vs the baseline. The detectors are deterministic, so on the golden set this means no
  record may flip the wrong way.
- **Detector, absolute:** `secrets` and `pii` must catch 100% at ≤5% FPR; `research_note_schema`
  100% at 0% FPR.
- **End-to-end:** for `filters_taint` on the test split, ASR ≤ 15% and benign task success ≥ 80%.
- Latency is reported, not gated (shared runners are noisy).

**Changing the numbers on purpose** (new detector, retuned threshold, new records/scenarios):
regenerate the baseline (and re-record the cassette) in the same PR, so the diff is reviewed:

```bash
uv run boundary-eval detectors --suite golden --repeats 20 --quiet --out packages/eval/baselines/golden.json
uv run boundary-eval e2e --mode record --out packages/eval/results/e2e.json   # needs an LLM key
```

## Secrets and setup (one-time, needs repo admin)

Step by step in [DEPLOY.md, part 3](DEPLOY.md#part-3-cicd):

- **`HF_TOKEN`**: a Hugging Face **read** token from an account that has accepted the Llama
  Prompt Guard 2 licence. Without it the detector gate can't download the production model.
- **`OPENAI_API_KEY`** (or `ANTHROPIC_API_KEY` / `GEMINI_API_KEY`): only for the nightly live job.
- **Actions settings:** allow Actions to create pull requests (the nightly cassette refresh opens one).
- **A ruleset on `main`**: PRs required, and the `test` and `web` checks must pass. This is what
  makes a failing gate actually block a merge.

## Deploy (CD)

`.github/workflows/deploy.yml` runs after `ci` succeeds on a push to `main`, or by hand (*Run
workflow*, with an optional commit to roll back to).

1. It assumes an AWS role through GitHub's OIDC token. There's no stored AWS key or SSH key, and
   the role can only send a command to the one server.
2. It runs `infra/deploy.sh` on the server through Systems Manager, at that exact commit.
3. It smoke-tests the live site's access rules (`infra/smoke-test.sh`).

It does nothing until the `production` environment is configured ([DEPLOY.md, steps
18–19](DEPLOY.md#18-cd-the-aws-side-once)).

## Nightly live run

[`.github/workflows/nightly-live.yml`](../.github/workflows/nightly-live.yml) runs the end-to-end
scenarios against the real model, re-records the cassette (filling only gaps), and opens a PR if it
changed — so model drift shows up as a reviewable diff rather than a silent CI break. It skips
cleanly when no LLM key secret is set.

## Reproducing the "blocked PR" screenshot

The README should show a PR blocked by an eval regression. To produce one: open a PR with a change
that lowers a guarded number, e.g. trim the `secrets` ruleset in `policies/rules/secrets.v1.yaml`
to a few rules. The detector gate then fails and the sticky comment shows the drop:

```
## Guard eval vs baseline (test split)

❌ **Eval gate failed**

| | Policy | Catch rate | FPR | Pos / Neg |
| ❌ | `secrets` | 66.7% (-33.3pp ⚠) | 0.0% | 6 / 34 |
...
**Failures**
- secrets: catch rate 66.7% is below the floor 100.0%
- secrets: catch rate dropped 33.3pp (100.0% → 66.7%)
```

Locally, the same check: `boundary-eval compare packages/eval/baselines/golden.json <regressed-run>.json`
exits 1 and prints that table.
