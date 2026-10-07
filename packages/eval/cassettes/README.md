# LLM cassettes

`e2e.json` records the planner's LLM responses for the end-to-end scenarios, keyed by a hash of
the request (model, messages, tools, temperature). It lets CI replay the agent runs deterministically
with no API key and no network.

**Record it** (needs a key for the model in the root `.env`, e.g. `OPENAI_API_KEY`):

```bash
uv run boundary-eval e2e --mode record --out packages/eval/results/e2e.json
```

Replay needs no key: the command gives the planner a placeholder, and the cassette answers every
call (a miss fails the run before any request is made). Two things keep the keys stable from one
run to the next: the per-request spotlight nonce is normalised out of the key, and message
timestamps have microsecond precision, so the planner's history window is always the same messages.
Guard timeouts are lifted during the run (as in the detector eval), so verdicts don't depend on
machine speed; pass `--enforce-timeouts` to keep them.

Re-recording fills only missing keys, so it is cheap after a prompt change. Commit the updated
`e2e.json` in the same PR as the change that altered the prompts (spotlighting, a new scenario, a
model bump), so reviewers see the diff. `--mode live` runs against the model without recording;
`--mode replay` (the default, used in CI) fails on any missing key.
