"""Best-effort ONNX export for CPU latency. Optional: the guard loads the HF model directly, so the
ONNX file is a latency optimisation, not required for the detector to work."""

from __future__ import annotations

from pathlib import Path


def export_onnx(model_dir: Path, out_path: Path | None = None) -> Path:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    out_path = out_path or model_dir / "model.onnx"
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
    model = AutoModelForSequenceClassification.from_pretrained(str(model_dir))
    model.eval()

    dummy = tokenizer("export sample", return_tensors="pt", truncation=True, max_length=512)
    inputs = (dummy["input_ids"], dummy["attention_mask"])
    dynamic = {name: {0: "batch", 1: "seq"} for name in ("input_ids", "attention_mask", "logits")}
    with torch.no_grad():
        torch.onnx.export(
            model,
            inputs,
            str(out_path),
            input_names=["input_ids", "attention_mask"],
            output_names=["logits"],
            dynamic_axes=dynamic,
            opset_version=17,
        )
    return out_path
