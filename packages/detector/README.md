# boundary-detector

The injection detector we train ourselves, aimed at the gap the off-the-shelf models leave:
**indirect prompt injection buried in tool output** (see [docs/EVAL.md](../../docs/EVAL.md)). A small
encoder (DeBERTa-v3-xsmall) fine-tuned as a binary classifier and loaded by the guard's
`hf_classifier` detector, so no new runtime code is needed.

Result on the tool-output test split: 82.8% catch at 9.5% false positives (threshold 0.71), against
52.9% / 27.9% for ProtectAI and 54.5% / 20.4% for Prompt Guard 2. Details and caveats:
[MODELCARD.md](MODELCARD.md).

## Run

```bash
uv sync                                                       # the dev group includes the training deps
uv run boundary-detector data --out packages/detector/data    # assemble train/dev/test (train split only)
uv run boundary-detector train --out packages/detector/model  # fine-tune (GPU recommended; CPU takes hours)
uv run boundary-detector evaluate packages/detector/model     # ours vs the off-the-shelf baselines
uv run boundary-detector export packages/detector/model       # ONNX for CPU latency
```

Training on a free Colab or Kaggle T4 takes minutes: [colab/README.md](colab/README.md).

Training data comes only from the datasets' `train` split plus clean carrier documents; the dev/test
tool-output records are never seen in training (checked by content hash). The policy that uses the
model is `policies/experiments/our_detector.yaml`; it is not in the production `guard.yaml` until the
model is hosted where CI can fetch it.

## Test

```bash
uv run pytest packages/detector/tests
```

## Key files (`src/boundary_detector/`)

`cli.py` (commands), `data.py` (assembly and leakage check), `train.py`, `evaluate.py`, `export.py`,
`report.py`.
