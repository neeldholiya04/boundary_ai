from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from boundary_guard.core.detector import Detector, register_detector
from boundary_guard.core.types import CheckContext, Detection
from boundary_guard.detectors._models import require_ml, shared, torch_device


def _load(model: str, revision: str | None) -> tuple[Any, Any]:
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model, revision=revision)
    net = AutoModelForSequenceClassification.from_pretrained(model, revision=revision)
    net.to(torch_device())
    net.eval()
    return tokenizer, net


class HFClassifierDetector(Detector):
    """Sequence classifier from the Hugging Face Hub (prompt injection, toxicity, ...).

    Long inputs are split into overlapping token windows and each window is scored; the
    text's score is the maximum over windows, so an instruction buried deep in a page still
    counts. Beyond `max_chunks` windows, the first and last halves are kept (attackers favour
    the end of a document) and the reason says the middle was skipped.

    `positive_labels` names the classifier labels that count as "detected". With
    `multi_label: true` each label gets an independent sigmoid and the score is the max over
    positive labels; otherwise softmax probabilities of positive labels are summed.
    """

    type_name = "hf_classifier"

    def __init__(
        self,
        model: str,
        *,
        revision: str | None,
        threshold: float,
        positive_labels: list[str],
        multi_label: bool = False,
        max_tokens: int = 512,
        overlap_tokens: int = 64,
        max_chunks: int = 32,
        batch_size: int = 8,
    ) -> None:
        require_ml()
        self.model_name = model
        self.revision = revision
        self.threshold = threshold
        self.multi_label = multi_label
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        self.max_chunks = max_chunks
        self.batch_size = batch_size
        self.loaded = shared(("hf_classifier", model, revision or "main"), lambda: _load(model, revision))

        tokenizer, net = self.loaded.obj
        id2label = {int(i): str(label) for i, label in net.config.id2label.items()}
        wanted = {label.lower() for label in positive_labels}
        self.positive_ids = sorted(i for i, label in id2label.items() if label.lower() in wanted)
        if not self.positive_ids:
            raise ValueError(
                f"{model}: none of {positive_labels} in model labels {sorted(id2label.values())}"
            )
        self.positive_labels = [id2label[i] for i in self.positive_ids]
        self._body_tokens = max_tokens - tokenizer.num_special_tokens_to_add(pair=False)
        self._score_texts(["warm-up"])  # first call is slow; pay it at load time

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        return await asyncio.to_thread(self._detect_sync, text)

    def _detect_sync(self, text: str) -> Detection:
        chunks, skipped = self._chunks(text)
        scores = self._score_texts(chunks)
        best = max(range(len(scores)), key=scores.__getitem__)
        score = scores[best]
        short = self.model_name.rsplit("/", 1)[-1]
        reasons = [f"{short} p={score:.3f} (threshold {self.threshold})"]
        if len(chunks) > 1:
            reasons.append(f"max over {len(chunks)} chunks at chunk {best + 1}")
        if skipped:
            reasons.append(f"input too long: middle {skipped} chunks not checked")
        return Detection(triggered=score >= self.threshold, score=score, reasons=reasons)

    def _chunks(self, text: str) -> tuple[list[str], int]:
        tokenizer, _ = self.loaded.obj
        # Tokenised only to measure and split; never fed to the model at this length.
        ids = tokenizer(text, add_special_tokens=False, truncation=False, verbose=False)["input_ids"]
        if len(ids) <= self._body_tokens:
            return [text], 0
        step = max(1, self._body_tokens - self.overlap_tokens)
        windows = []
        for start in range(0, len(ids), step):
            windows.append(ids[start : start + self._body_tokens])
            if start + self._body_tokens >= len(ids):
                break
        skipped = 0
        if len(windows) > self.max_chunks:
            head = self.max_chunks // 2
            tail = self.max_chunks - head
            skipped = len(windows) - self.max_chunks
            windows = windows[:head] + windows[-tail:]
        return [tokenizer.decode(w) for w in windows], skipped

    def _score_texts(self, texts: list[str]) -> list[float]:
        import torch

        tokenizer, net = self.loaded.obj
        scores: list[float] = []
        with self.loaded.lock, torch.inference_mode():
            for i in range(0, len(texts), self.batch_size):
                batch = texts[i : i + self.batch_size]
                enc = tokenizer(
                    batch, truncation=True, max_length=self.max_tokens, padding=True, return_tensors="pt"
                ).to(torch_device())
                logits = net(**enc).logits
                if self.multi_label:
                    probs = torch.sigmoid(logits)[:, self.positive_ids].max(dim=-1).values
                else:
                    probs = torch.softmax(logits, dim=-1)[:, self.positive_ids].sum(dim=-1)
                scores.extend(float(p) for p in probs)
        return scores

    def fingerprint(self) -> str:
        return (
            f"{self.model_name}@{self.revision}:{','.join(self.positive_labels)}:{self.multi_label}:"
            f"{self.max_tokens}/{self.overlap_tokens}/{self.max_chunks}"
        )


@register_detector("hf_classifier")
def _factory(params: dict[str, Any], base_dir: Path) -> Detector:
    chunking = params.get("chunking", {})
    return HFClassifierDetector(
        params["model"],
        revision=params.get("revision"),
        threshold=float(params["threshold"]),
        positive_labels=list(params["positive_labels"]),
        multi_label=bool(params.get("multi_label", False)),
        max_tokens=int(chunking.get("max_tokens", 512)),
        overlap_tokens=int(chunking.get("overlap_tokens", 64)),
        max_chunks=int(chunking.get("max_chunks", 32)),
        batch_size=int(params.get("batch_size", 8)),
    )
