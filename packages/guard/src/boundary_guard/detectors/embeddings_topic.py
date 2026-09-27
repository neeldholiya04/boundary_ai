from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from boundary_guard.core.detector import Detector, file_digest, register_detector
from boundary_guard.core.types import CheckContext, Detection
from boundary_guard.detectors._models import require_ml, shared, torch_device


def _load(model: str, revision: str | None) -> Any:
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model, revision=revision, device=torch_device())


def _exemplars(path: Path) -> list[str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = [str(x).strip() for x in raw.get("exemplars", []) if str(x).strip()]
    if not items:
        raise ValueError(f"{path}: no exemplars")
    return items


class EmbeddingTopicDetector(Detector):
    """Off-topic detection by nearest exemplar.

    The request is embedded and compared (cosine) with allow and deny exemplars. It triggers
    when the closest deny exemplar beats the closest allow exemplar by more than `margin`.
    Score = max_deny - max_allow, so the threshold is the margin.
    """

    type_name = "embeddings_topic"

    def __init__(self, model: str, *, revision: str | None, allow: Path, deny: Path, margin: float) -> None:
        require_ml()
        self.model_name = model
        self.revision = revision
        self.allow_path, self.deny_path = allow, deny
        self.threshold = margin
        self.loaded = shared(
            ("sentence_transformer", model, revision or "main"), lambda: _load(model, revision)
        )
        self.allow_texts = _exemplars(allow)
        self.deny_texts = _exemplars(deny)
        self.allow_emb = self._embed(self.allow_texts)
        self.deny_emb = self._embed(self.deny_texts)

    def _embed(self, texts: list[str]) -> Any:
        with self.loaded.lock:
            return self.loaded.obj.encode(texts, normalize_embeddings=True, convert_to_tensor=True)

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        return await asyncio.to_thread(self._detect_sync, text)

    def _detect_sync(self, text: str) -> Detection:
        query = self._embed([text])[0]
        allow_sims = self.allow_emb @ query
        deny_sims = self.deny_emb @ query
        a_idx, d_idx = int(allow_sims.argmax()), int(deny_sims.argmax())
        best_allow, best_deny = float(allow_sims[a_idx]), float(deny_sims[d_idx])
        score = best_deny - best_allow
        return Detection(
            triggered=score > self.threshold,
            score=score,
            reasons=[
                f"closest deny {best_deny:.3f} ({self.deny_texts[d_idx]!r}) vs "
                f"allow {best_allow:.3f} ({self.allow_texts[a_idx]!r}); margin {self.threshold}"
            ],
        )

    def fingerprint(self) -> str:
        return (
            f"{self.model_name}@{self.revision}:{file_digest(self.allow_path)}:{file_digest(self.deny_path)}"
        )


@register_detector("embeddings_topic")
def _factory(params: dict[str, Any], base_dir: Path) -> Detector:
    return EmbeddingTopicDetector(
        params["model"],
        revision=params.get("revision"),
        allow=base_dir / params["allow"],
        deny=base_dir / params["deny"],
        margin=float(params.get("margin", 0.0)),
    )
