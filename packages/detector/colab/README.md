# Training the detector on a free GPU (Colab / Kaggle)

CPU training takes hours; a free T4 does it in minutes. The notebook
[`train_detector.ipynb`](train_detector.ipynb) is self-contained.

1. **Assemble the data locally** (once):
   ```bash
   uv run boundary-detector data --out packages/detector/data
   ```
   This writes `packages/detector/data/train.jsonl` and `val.jsonl` (train split only; leakage-checked).
2. **Open the notebook in Colab** → Runtime → Change runtime type → **T4 GPU**.
3. Run the cells. When prompted, **upload `train.jsonl` and `val.jsonl`** from `packages/detector/data/`.
4. Training runs ~3 epochs (a few minutes) and downloads `boundary-detector-model.zip`.
5. **Unzip into the repo:**
   ```bash
   rm -rf packages/detector/model && unzip boundary-detector-model.zip -d packages/detector/model
   ```
6. **Evaluate** ours vs the off-the-shelf baselines on the tool-output test split:
   ```bash
   uv run boundary-detector evaluate packages/detector/model --out packages/eval/results/detector.json
   ```
7. To use it in the guard, point a policy at `packages/detector/model` (see
   `policies/experiments/our_detector.yaml`) and run from the repo root.

Kaggle works the same way (enable a GPU accelerator; use its file upload). The training code matches
`boundary-detector train`, so a GPU box with the repo can instead just run that command directly.
