from __future__ import annotations

from boundary_guard.core.types import Span


def merge_spans(spans: list[Span]) -> list[Span]:
    """Merge overlapping or touching spans. The earliest-starting span's label wins,
    ties broken by the longer span, so a merged region gets a single placeholder."""
    ordered = sorted(spans, key=lambda s: (s.start, -(s.end - s.start)))
    merged: list[Span] = []
    for span in ordered:
        if span.end <= span.start:
            continue
        if merged and span.start <= merged[-1].end:
            last = merged[-1]
            merged[-1] = Span(last.start, max(last.end, span.end), last.label)
        else:
            merged.append(span)
    return merged


def redact(text: str, spans: list[Span]) -> str:
    """Replace spans with typed placeholders like <EMAIL_1>.

    The same value under the same label gets the same placeholder within one text,
    so the model can still tell that two mentions refer to one entity.
    """
    merged = merge_spans(spans)
    if not merged:
        return text

    numbering: dict[tuple[str, str], int] = {}
    counters: dict[str, int] = {}
    out: list[str] = []
    cursor = 0
    for span in merged:
        value = text[span.start : span.end]
        key = (span.label, value)
        if key not in numbering:
            counters[span.label] = counters.get(span.label, 0) + 1
            numbering[key] = counters[span.label]
        out.append(text[cursor : span.start])
        out.append(f"<{span.label}_{numbering[key]}>")
        cursor = span.end
    out.append(text[cursor:])
    return "".join(out)
