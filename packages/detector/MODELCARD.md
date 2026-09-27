# Model card: boundary tool-output injection detector

Trained on a free Colab T4. Results below are on the tool-output injection **test** split.

## What it is

A binary sequence classifier (`BENIGN` / `INJECTION`) fine-tuned from **microsoft/deberta-v3-xsmall**
to detect **indirect prompt injection buried in tool output** — the gap the off-the-shelf detectors
leave (ProtectAI and Prompt Guard 2 catch a minority of plain, in-context injections; see
docs/EVAL.md). It is loaded by the guard's existing `hf_classifier` detector, so it needs no new
runtime code.

## Intended use

The `tool_output` stage of the boundary guard, as an alternative or companion to the off-the-shelf
injection detectors. Not a general prompt-injection classifier: it is trained on tool-output-shaped
text and evaluated only there.

## Training data

Assembled by `boundary-detector data` **from the datasets' `train` split only**:
- InjecAgent train tool-response templates (positives) and their benign fills (negatives).
- Clean carrier documents (HaluEval train news articles) with a train-split injection instruction
  spliced in at a random depth, wrapped as real indirect injection hides (HTML comment, hidden
  span, "note to AI", code comment, footer, plain paragraph). The injected instructions are existing
  vetted train-split records — no new attack prose is authored.

Roughly 1.2k examples (~2:1 positive:negative). The dev/test tool-output injection records used for
evaluation are **never** in training, checked by content hash (`assemble` raises on any overlap).

## Evaluation

`boundary-detector evaluate` scores it against ProtectAI and Prompt Guard 2 on the tool-output
injection **test** split (golden + extended). Each detector is tuned on the dev split at a matched
~10% false-positive budget, then reported on test:

| Detector | Threshold | Catch rate | FPR | p50 / p99 ms |
|---|---|---|---|---|
| **ours** (DeBERTa-v3-xsmall, fine-tuned) | 0.71 | 82.8% [78–87] | 9.5% [6–15] | 76 / 378 |
| ProtectAI deberta-v3-base | 0.976 | 52.9% [47–59] | 27.9% [21–36] | 162 / 926 |
| Prompt Guard 2 86M | 0.00115 | 54.5% [48–61] | 20.4% [15–28] | 154 / 899 |

Test split: 244 injections / 147 clean tool-output records. Thresholds tuned on dev at ~10% FPR.

Ours has the **highest catch rate and the lowest false-positive rate** of the three on tool output,
and is the fastest (smallest model). Its dev-tuned threshold also transferred to test (dev FPR 9.7%
→ test 9.5%), while the baselines' thresholds did not (their test FPR jumped to 20–28%). Full
context: [docs/EVAL.md](../../docs/EVAL.md).

## Limitations and honest caveats

- Small model (~70M params) and a small (~1.2k) training set — treat it as a demonstration of the
  *pipeline and the honest comparison*, not a hardened production detector. The scores overlap
  (attacks ~0.65–0.83, clean ~0.62–0.81), so it needs a tuned threshold (~0.71), and the margin is thin.
- Augmentation teaches "clean document + spliced instruction". Injection styles unlike the training
  wrappers, or non-English text, are out of scope.
- Carrier documents are news articles, not web pages / issues / files; a domain gap from real tool
  output remains. The extended InjecAgent test records mitigate but do not remove it.
- Trained and evaluated only for tool output; do not use it on user input.

## Reproduce

```bash
uv run boundary-detector data --out packages/detector/data
uv run boundary-detector train --out packages/detector/model      # GPU recommended
uv run boundary-detector evaluate packages/detector/model --out packages/eval/results/detector.json
```

Base model `microsoft/deberta-v3-xsmall` is Apache/MIT-style licensed (see its model card). This
fine-tune inherits the base licence and the training data licences (docs/EVAL.md sources).
