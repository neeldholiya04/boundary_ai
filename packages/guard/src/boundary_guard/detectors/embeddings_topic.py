from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from boundary_guard.core.detector import Detector, register_detector
from boundary_guard.core.types import CheckContext, Detection
from boundary_guard.detectors._models import require_ml, shared, torch_device

_WORD = re.compile(r"[^\W\d_]{2,}")  # a word: two or more letters, in any script


def _load(model: str, revision: str | None) -> Any:
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model, revision=revision, device=torch_device())


def _inline(items: list[str], side: str) -> list[str]:
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    if not cleaned:
        raise ValueError(f"embeddings_topic: no {side} exemplars")
    return cleaned


def _exemplars(path: Path) -> list[str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = [str(x).strip() for x in raw.get("exemplars", []) if str(x).strip()]
    if not items:
        raise ValueError(f"{path}: no exemplars")
    return items


class EmbeddingTopicDetector(Detector):
    """Off-topic detection by nearest exemplar.

    The request is embedded and compared (cosine) with allow and deny exemplars. It triggers
    when the closest deny exemplar beats the closest allow exemplar by more than `margin` AND is
    itself at least `min_similarity` alike. Without the floor, a short message that resembles
    nothing ("hello", "ok continue") lands nearest some deny exemplar by a hair and is blocked.
    Score = max_deny - max_allow, so the threshold is the margin.
    """

    type_name = "embeddings_topic"

    def __init__(
        self,
        model: str,
        *,
        revision: str | None,
        allow: Path | list[str],
        deny: Path | list[str],
        margin: float,
        min_similarity: float = 0.0,
        min_words: int = 0,
    ) -> None:
        # Exemplars come from files (the shipped topic lists) or inline (rules written in the dashboard).
        require_ml()
        self.model_name = model
        self.revision = revision
        self.threshold = margin
        self.min_similarity = min_similarity
        self.min_words = min_words
        self.loaded = shared(
            ("sentence_transformer", model, revision or "main"), lambda: _load(model, revision)
        )
        self.allow_texts = _exemplars(allow) if isinstance(allow, Path) else _inline(allow, "allow")
        self.deny_texts = _exemplars(deny) if isinstance(deny, Path) else _inline(deny, "deny")
        self.allow_emb = self._embed(self.allow_texts)
        self.deny_emb = self._embed(self.deny_texts)

    def _embed(self, texts: list[str]) -> Any:
        with self.loaded.lock:
            return self.loaded.obj.encode(texts, normalize_embeddings=True, convert_to_tensor=True)

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        return await asyncio.to_thread(self._detect_sync, text)

    def _detect_sync(self, text: str) -> Detection:
        # A message with almost no words ("123", "ok", "yes") has no topic; its embedding lands near
        # whatever it vaguely resembles ("123" near "Do my math homework"). Not judged.
        words = len(_WORD.findall(text))
        if words < self.min_words:
            return Detection(triggered=False, score=None, reasons=[f"{words} words: too short to judge"])
        query = self._embed([text])[0]
        allow_sims = self.allow_emb @ query
        deny_sims = self.deny_emb @ query
        a_idx, d_idx = int(allow_sims.argmax()), int(deny_sims.argmax())
        best_allow, best_deny = float(allow_sims[a_idx]), float(deny_sims[d_idx])
        score = best_deny - best_allow
        return Detection(
            triggered=score > self.threshold and best_deny >= self.min_similarity,
            score=score,
            reasons=[
                f"closest deny {best_deny:.3f} ({self.deny_texts[d_idx]!r}) vs "
                f"allow {best_allow:.3f} ({self.allow_texts[a_idx]!r}); margin {self.threshold}, "
                f"min similarity {self.min_similarity}"
            ],
        )

    def fingerprint(self) -> str:
        allow = hashlib.sha256("\n".join(self.allow_texts).encode()).hexdigest()[:16]
        deny = hashlib.sha256("\n".join(self.deny_texts).encode()).hexdigest()[:16]
        limits = f"min={self.min_similarity}:words={self.min_words}"
        return f"{self.model_name}@{self.revision}:{allow}:{deny}:{limits}"


@register_detector("embeddings_topic")
def _factory(params: dict[str, Any], base_dir: Path) -> Detector:
    def side(name: str) -> Path | list[str]:
        # `<side>_exemplars` (inline list) takes precedence; else `<side>` names a file. A rule may mix:
        # inline deny exemplars against the shipped allow list.
        inline = params.get(f"{name}_exemplars")
        if inline is not None:
            return list(inline)
        return base_dir / params[name]

    return EmbeddingTopicDetector(
        params["model"],
        revision=params.get("revision"),
        allow=side("allow"),
        deny=side("deny"),
        margin=float(params.get("margin", 0.0)),
        min_similarity=float(params.get("min_similarity", 0.0)),
        min_words=int(params.get("min_words", 0)),
    )
