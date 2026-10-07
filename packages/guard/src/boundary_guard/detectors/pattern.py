from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from pathlib import Path
from typing import Any

import regex

from boundary_guard.core.detector import Detector, register_detector
from boundary_guard.core.types import CheckContext, Detection, Span

_FLAGS = {"i": regex.IGNORECASE, "m": regex.MULTILINE, "s": regex.DOTALL}
# Patterns written in the dashboard are not reviewed like the shipped rulesets, so a pathological one
# (catastrophic backtracking) must not stall a request: all of a check's searches share one time
# budget, and running out raises, which the policy's on_error turns into a verdict.
DEFAULT_SEARCH_TIMEOUT_S = 0.25
MAX_PATTERNS = 50
MAX_PATTERN_CHARS = 500
# Counted repetition is expanded at compile time: `(?:a{60000}){60000}` is 20 characters and needs
# gigabytes. Cap each count and their product across the pattern (an upper bound on nesting).
MAX_REPEAT = 1000
MAX_REPEAT_PRODUCT = 100_000
_COUNT = re.compile(r"(?<!\\)\{(\d+)(?:,(\d*))?\}")


def _check_repeats(pattern: str) -> None:
    counts = [max(int(lo), int(hi) if hi else int(lo)) for lo, hi in _COUNT.findall(pattern)]
    if any(c > MAX_REPEAT for c in counts):
        raise ValueError(f"pattern: repetition counts are limited to {MAX_REPEAT} ({pattern!r})")
    if math.prod(c for c in counts if c > 1) > MAX_REPEAT_PRODUCT:
        raise ValueError(f"pattern: nested repetition is too large ({pattern!r})")


class PatternTimeout(RuntimeError):
    """A pattern ran out of its search budget. Not a TimeoutError: the pipeline reads that as the
    policy's own timeout, and the decision log should say which one it was."""


class PatternDetector(Detector):
    """Inline regular expressions for operator-written rules (the shipped rulesets use `regex_rules`)."""

    type_name = "pattern"

    def __init__(
        self,
        patterns: list[str],
        *,
        flags: list[str] | None = None,
        label: str = "MATCH",
        timeout_s: float = DEFAULT_SEARCH_TIMEOUT_S,
    ) -> None:
        cleaned = [p for p in patterns if p]
        if not cleaned:
            raise ValueError("pattern: at least one pattern is required")
        if len(cleaned) > MAX_PATTERNS:
            raise ValueError(f"pattern: at most {MAX_PATTERNS} patterns")
        compiled_flags = 0
        for name in flags or []:
            if name not in _FLAGS:
                raise ValueError(f"pattern: unknown flag {name!r} (known: {', '.join(_FLAGS)})")
            compiled_flags |= _FLAGS[name]
        self.compiled = []
        for p in cleaned:
            if len(p) > MAX_PATTERN_CHARS:
                raise ValueError(f"pattern: patterns are limited to {MAX_PATTERN_CHARS} characters")
            _check_repeats(p)
            try:
                self.compiled.append(regex.compile(p, compiled_flags))
            except regex.error as exc:
                raise ValueError(f"pattern: invalid regular expression {p!r}: {exc}") from exc
        self.patterns = cleaned
        self.flags = sorted(flags or [])
        self.label = label
        self.timeout_s = timeout_s

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        # Off the event loop: the timeout bounds the work, but a thread keeps other requests moving.
        return await asyncio.to_thread(self._scan, text)

    def _scan(self, text: str) -> Detection:
        spans: list[Span] = []
        hit: dict[int, None] = {}  # pattern indexes that matched, in order
        deadline = time.monotonic() + self.timeout_s
        for i, compiled in enumerate(self.compiled):
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise TimeoutError
                matches = list(compiled.finditer(text, timeout=remaining))
            except TimeoutError as exc:
                raise PatternTimeout(
                    f"patterns exceeded their {self.timeout_s}s budget at {self.patterns[i]!r}"
                ) from exc
            for m in matches:
                if m.end() > m.start():
                    spans.append(Span(m.start(), m.end(), self.label))
                    hit[i] = None
        return Detection(
            triggered=bool(spans),
            score=1.0 if spans else 0.0,
            spans=spans,
            reasons=[f"pattern {self.patterns[i]!r}" for i in list(hit)[:5]],
        )

    def fingerprint(self) -> str:
        digest = hashlib.sha256("\n".join(self.patterns).encode()).hexdigest()[:16]
        return f"pattern:{digest}:{','.join(self.flags)}"


@register_detector("pattern")
def _factory(params: dict[str, Any], base_dir: Path) -> Detector:
    return PatternDetector(
        list(params["patterns"]),
        flags=list(params.get("flags", [])),
        label=str(params.get("label", "MATCH")),
        timeout_s=float(params.get("timeout_s", DEFAULT_SEARCH_TIMEOUT_S)),
    )
