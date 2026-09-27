"""Fine-tune a small encoder as a binary tool-output-injection classifier.

DeBERTa-v3-xsmall by default (~22M params, ungated). Runs on CPU (slow) or GPU. The saved model
is a standard HF sequence classifier with labels {BENIGN, INJECTION}, so the guard loads it with
the existing `hf_classifier` detector — no new runtime code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_MODEL = "microsoft/deberta-v3-xsmall"
ID2LABEL = {0: "BENIGN", 1: "INJECTION"}
LABEL2ID = {v: k for k, v in ID2LABEL.items()}


@dataclass(slots=True)
class TrainConfig:
    base_model: str = BASE_MODEL
    epochs: float = 3.0
    batch_size: int = 8
    learning_rate: float = 2e-5
    max_length: int = 512
    seed: int = 20260926
    warmup_ratio: float = 0.1
    weight_decay: float = 0.01


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _metrics(eval_pred) -> dict[str, float]:
    import numpy as np
    from sklearn.metrics import f1_score, precision_score, recall_score

    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    return {
        "f1": float(f1_score(labels, preds, zero_division=0)),
        "precision": float(precision_score(labels, preds, zero_division=0)),
        "recall": float(recall_score(labels, preds, zero_division=0)),
    }


def train(data_dir: Path, out_dir: Path, config: TrainConfig | None = None) -> dict[str, Any]:
    import torch
    from datasets import Dataset
    from transformers import (
        AutoModelForSequenceClassification,
        AutoTokenizer,
        DataCollatorWithPadding,
        Trainer,
        TrainingArguments,
        set_seed,
    )

    config = config or TrainConfig()
    set_seed(config.seed)

    train_rows = _load(data_dir / "train.jsonl")
    val_rows = _load(data_dir / "val.jsonl")
    tokenizer = AutoTokenizer.from_pretrained(config.base_model)

    def to_ds(rows: list[dict[str, Any]]) -> Any:
        ds = Dataset.from_dict({"text": [r["text"] for r in rows], "labels": [r["label"] for r in rows]})
        return ds.map(
            lambda b: tokenizer(b["text"], truncation=True, max_length=config.max_length),
            batched=True,
            remove_columns=["text"],
        )

    train_ds, val_ds = to_ds(train_rows), to_ds(val_rows)
    model = AutoModelForSequenceClassification.from_pretrained(
        config.base_model, num_labels=2, id2label=ID2LABEL, label2id=LABEL2ID
    )

    args = TrainingArguments(
        output_dir=str(out_dir / "checkpoints"),
        num_train_epochs=config.epochs,
        per_device_train_batch_size=config.batch_size,
        per_device_eval_batch_size=config.batch_size * 2,
        learning_rate=config.learning_rate,
        warmup_ratio=config.warmup_ratio,
        weight_decay=config.weight_decay,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="f1",
        greater_is_better=True,
        logging_steps=25,
        seed=config.seed,
        use_cpu=not torch.cuda.is_available(),
        report_to=[],
    )
    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        processing_class=tokenizer,
        data_collator=DataCollatorWithPadding(tokenizer),
        compute_metrics=_metrics,
    )
    trainer.train()
    eval_metrics = trainer.evaluate()

    out_dir.mkdir(parents=True, exist_ok=True)
    trainer.save_model(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))
    card = {
        "base_model": config.base_model,
        "epochs": config.epochs,
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "train_size": len(train_rows),
        "val_size": len(val_rows),
        "val_metrics": {k: round(v, 4) for k, v in eval_metrics.items() if k.startswith("eval_")},
    }
    (out_dir / "training_summary.json").write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    return card
