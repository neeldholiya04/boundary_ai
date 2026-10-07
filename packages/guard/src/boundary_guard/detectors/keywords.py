from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from boundary_guard.core.detector import Detector, register_detector
from boundary_guard.core.types import CheckContext, Detection, Span


class KeywordsDetector(Detector):
    """Words or phrases, matched literally (no regex syntax), for rules written by an operator.

    Matching is case-insensitive and on whole words by default, so "card" does not fire on "discard".
    Every occurrence becomes a span, so a `redact` policy blanks each one.
    """

    type_name = "keywords"

    def __init__(
        self,
        keywords: list[str],
        *,
        case_sensitive: bool = False,
        whole_word: bool = True,
        label: str = "KEYWORD",
    ) -> None:
        cleaned = sorted({k.strip() for k in keywords if k and k.strip()}, key=len, reverse=True)
        if not cleaned:
            raise ValueError("keywords: at least one non-empty keyword is required")
        self.keywords = cleaned
        self.case_sensitive = case_sensitive
        self.whole_word = whole_word
        self.label = label
        # Longest first, so "api key" wins over "key" when both are listed.
        body = "|".join(re.escape(k) for k in cleaned)
        if whole_word:
            body = rf"(?<!\w)(?:{body})(?!\w)"
        self.pattern = re.compile(body, 0 if case_sensitive else re.IGNORECASE)

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        spans = [Span(m.start(), m.end(), self.label) for m in self.pattern.finditer(text)]
        found = sorted({text[s.start : s.end].lower() for s in spans})
        return Detection(
            triggered=bool(spans),
            score=1.0 if spans else 0.0,
            spans=spans,
            reasons=[f"keyword {k!r}" for k in found[:5]],
        )

    def fingerprint(self) -> str:
        digest = hashlib.sha256("\n".join(self.keywords).encode()).hexdigest()[:16]
        return f"keywords:{digest}:{self.case_sensitive}:{self.whole_word}"


class AlwaysDetector(Detector):
    """Fires on every check. Combined with stages and `tools`, it turns a policy into a plain
    "this tool call needs approval / is blocked" rule, so tool rules live in the same place as
    content rules."""

    type_name = "always"

    def __init__(self, reason: str = "matches every check") -> None:
        self.reason = reason

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        return Detection(triggered=True, score=1.0, reasons=[self.reason])

    def fingerprint(self) -> str:
        return f"always:{self.reason}"


@register_detector("keywords")
def _keywords(params: dict[str, Any], base_dir: Path) -> Detector:
    return KeywordsDetector(
        list(params["keywords"]),
        case_sensitive=bool(params.get("case_sensitive", False)),
        whole_word=bool(params.get("whole_word", True)),
        label=str(params.get("label", "KEYWORD")),
    )


@register_detector("always")
def _always(params: dict[str, Any], base_dir: Path) -> Detector:
    return AlwaysDetector(str(params.get("reason", "matches every check")))
