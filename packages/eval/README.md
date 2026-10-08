# boundary-eval

Datasets, metrics, harnesses and CI gates that measure `boundary-guard` and the agent. Method,
datasets and gates are described in [docs/EVAL.md](../../docs/EVAL.md); current numbers in
[docs/RESULTS.md](../../docs/RESULTS.md).

## Commands

From the repo root:

```bash
uv run boundary-eval validate packages/eval/datasets           # schema, duplicate ids, split leakage
uv run boundary-eval detectors --suite golden                  # score every policy (also: extended, all)
uv run boundary-eval compare packages/eval/baselines/golden.json packages/eval/results/run.json  # gates
uv run boundary-eval tune packages/eval/results/run.json --max-fpr 0.02   # dev threshold sweep
uv run boundary-eval e2e                                       # end-to-end scenarios, replayed (no key)
uv run boundary-eval loadtest --out packages/eval/baselines/loadtest.json
uv run boundary-eval build-extended                            # rebuild the extended set
uv run boundary-eval results                                   # regenerate docs/RESULTS.md + README block
uv run boundary-eval show gold-ui-tho-001                      # one record, placeholders expanded
```

`tune` skips `all_of` and `embeddings_topic` policies (not a single score against a threshold).
Regenerating baselines after an intended change: [docs/EVAL.md](../../docs/EVAL.md#regenerating-baselines).

## Layout

| Path | What |
|---|---|
| `datasets/golden/` | hand-written records per stage, fixtures, [authoring guide](datasets/golden/README.md) |
| `datasets/extended/` | public benchmarks and generated PII/secrets ([SOURCES.md](datasets/extended/SOURCES.md)) |
| `scenarios/` | end-to-end attack, benign and secrets scenarios |
| `cassettes/e2e.json` | recorded LLM responses for replay |
| `baselines/` | committed results the gates and RESULTS.md read |
| `gates.yaml` | detector and end-to-end gate thresholds |
| `results/` | local outputs (not committed) |
| `src/boundary_eval/` | `cli.py`, `runner.py` (detector eval), `metrics.py` (Wilson intervals), `compare.py` (gates), `tune.py`, `e2e/` (scenario harness, fixture MCP server, cassette), `extended/` (dataset builders), `loadtest.py`, `results.py`, `playground.py` (the Playground's attack mode), `fakes.py` (`{{fake:...}}` placeholders) |

## Test

```bash
uv run pytest packages/eval/tests
```
