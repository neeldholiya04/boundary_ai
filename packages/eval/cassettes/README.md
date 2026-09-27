# LLM cassettes

`e2e.json` records the planner's LLM responses for the end-to-end scenarios, keyed by a hash of
the request (model, messages, tools, temperature). It lets CI replay the agent runs deterministically
with no API key and no network.

**Record it** (needs a key for the model in the root `.env`, e.g. `OPENAI_API_KEY`):

```bash
uv run boundary-eval e2e --mode record --out packages/eval/results/e2e.json
```

Re-recording fills only missing keys, so it is cheap after a prompt change. Commit the updated
`e2e.json` in the same PR as the change that altered the prompts (spotlighting, a new scenario, a
model bump), so reviewers see the diff. `--mode live` runs against the model without recording;
`--mode replay` (the default, used in CI) fails on any missing key.
