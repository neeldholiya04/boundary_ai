from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from boundary_guard.core.detector import Detector, register_detector
from boundary_guard.core.types import CheckContext, Detection
from boundary_guard.detectors._models import require_ml, shared, torch_device

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def _load(model: str, revision: str | None) -> tuple[Any, Any]:
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model, revision=revision)
    net = AutoModelForSequenceClassification.from_pretrained(model, revision=revision)
    net.to(torch_device())
    net.eval()
    return tokenizer, net


def split_claims(text: str, min_chars: int = 12) -> list[str]:
    """Sentence-level claims; very short fragments are merged into the previous sentence."""
    claims: list[str] = []
    for part in _SENTENCE.split(text.strip()):
        part = part.strip()
        if not part:
            continue
        if claims and len(part) < min_chars:
            claims[-1] = f"{claims[-1]} {part}"
        else:
            claims.append(part)
    return claims or [text.strip()]


class NLIGroundednessDetector(Detector):
    """Is every claim in the answer supported by the reference texts (tool outputs)?

    Each sentence of the answer is a hypothesis; each reference chunk is a premise. A claim's
    support is its best entailment probability over all chunks. The answer's score is
    `1 - min(support)` with `aggregate: min` (how unsupported its weakest claim is) or
    `1 - mean(support)` with `aggregate: mean`. Triggers at `threshold`.

    Runs only when the check context carries `references`. If `metadata["question"]` is set
    (short QA answers), each claim is phrased as an answer to that question first.
    """

    type_name = "nli_groundedness"

    def __init__(
        self,
        model: str,
        *,
        revision: str | None,
        threshold: float,
        max_premise_tokens: int = 400,
        max_claims: int = 12,
        aggregate: str = "min",
    ) -> None:
        require_ml()
        if aggregate not in ("min", "mean"):
            raise ValueError(f"aggregate must be 'min' or 'mean', not {aggregate!r}")
        self.aggregate = aggregate
        self.model_name = model
        self.revision = revision
        self.threshold = threshold
        self.max_premise_tokens = max_premise_tokens
        self.max_claims = max_claims
        self.loaded = shared(("nli", model, revision or "main"), lambda: _load(model, revision))
        _, net = self.loaded.obj
        labels = {str(v).lower(): int(k) for k, v in net.config.id2label.items()}
        if "entailment" not in labels:
            raise ValueError(f"{model}: no 'entailment' label in {sorted(labels)}")
        self.entail_id = labels["entailment"]

    def applies(self, ctx: CheckContext) -> bool:
        return bool(ctx.references)

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        return await asyncio.to_thread(self._detect_sync, text, ctx)

    def _premises(self, references: list[str]) -> list[str]:
        tokenizer, _ = self.loaded.obj
        chunks: list[str] = []
        for ref in references:
            ids = tokenizer(ref, add_special_tokens=False, verbose=False)["input_ids"]
            step = self.max_premise_tokens
            for start in range(0, max(len(ids), 1), step):
                chunks.append(tokenizer.decode(ids[start : start + step]))
        return chunks

    def _detect_sync(self, text: str, ctx: CheckContext) -> Detection:
        import torch

        question = ctx.metadata.get("question")
        claims = split_claims(text)[: self.max_claims]
        hypotheses = [f"The answer to {question!r} is: {c}" if question else c for c in claims]
        premises = self._premises(ctx.references)
        tokenizer, net = self.loaded.obj

        pairs = [(p, h) for h in hypotheses for p in premises]
        with self.loaded.lock, torch.inference_mode():
            probs: list[float] = []
            for i in range(0, len(pairs), 16):
                batch = pairs[i : i + 16]
                enc = tokenizer(
                    [p for p, _ in batch],
                    [h for _, h in batch],
                    truncation="only_first",
                    max_length=512,
                    padding=True,
                    return_tensors="pt",
                ).to(torch_device())
                logits = net(**enc).logits
                probs.extend(float(x) for x in torch.softmax(logits, dim=-1)[:, self.entail_id])

        n = len(premises)
        support = [max(probs[i * n : (i + 1) * n]) for i in range(len(claims))]
        weakest = min(range(len(claims)), key=support.__getitem__)
        mean_support = sum(support) / len(support)
        score = 1.0 - (mean_support if self.aggregate == "mean" else support[weakest])
        reasons = [
            f"{self.aggregate} support {1.0 - score:.3f} over {len(claims)} claims; weakest "
            f"{support[weakest]:.3f}: {claims[weakest][:120]!r}"
        ]
        return Detection(triggered=score >= self.threshold, score=score, reasons=reasons)

    def fingerprint(self) -> str:
        return (
            f"{self.model_name}@{self.revision}:{self.max_premise_tokens}:{self.max_claims}:{self.aggregate}"
        )


@register_detector("nli_groundedness")
def _factory(params: dict[str, Any], base_dir: Path) -> Detector:
    return NLIGroundednessDetector(
        params["model"],
        revision=params.get("revision"),
        threshold=float(params["threshold"]),
        max_premise_tokens=int(params.get("max_premise_tokens", 400)),
        max_claims=int(params.get("max_claims", 12)),
        aggregate=str(params.get("aggregate", "min")),
    )
