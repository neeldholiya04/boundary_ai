# boundary-detector

The injection detector we train end-to-end, targeting the gap the off-the-shelf models leave:
**indirect prompt injection buried in tool output** (see docs/EVAL.md). It is a small encoder
(DeBERTa-v3-xsmall) fine-tuned as a binary classifier and loaded by the guard's `hf_classifier`
detector, so no new runtime code is needed.

```bash
uv sync                                            # installs the train extra
uv run boundary-detector data --out packages/detector/data  # assemble train/dev/test (train split only)
uv run boundary-detector train --out packages/detector/model # fine-tune (GPU recommended; CPU works, slow)
uv run boundary-detector evaluate packages/detector/model    # ours vs the off-the-shelf baselines
uv run boundary-detector export packages/detector/model      # ONNX for CPU latency
```

Training data is assembled only from the datasets' `train` split plus clean carrier documents;
the dev/test tool-output injection records are never seen in training (checked by content hash).
