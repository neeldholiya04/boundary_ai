from __future__ import annotations

from boundary_guard import Span
from boundary_guard.core.redact import merge_spans, redact


def test_merge_overlapping_and_touching_spans():
    merged = merge_spans([Span(5, 10, "B"), Span(0, 6, "A"), Span(10, 12, "C"), Span(20, 25, "D")])
    assert merged == [Span(0, 12, "A"), Span(20, 25, "D")]


def test_empty_spans_ignored():
    assert merge_spans([Span(3, 3, "A")]) == []


def test_same_value_gets_same_placeholder():
    text = "mail a@x.io then b@y.io then a@x.io"
    spans = [Span(5, 11, "EMAIL"), Span(17, 23, "EMAIL"), Span(29, 35, "EMAIL")]
    assert redact(text, spans) == "mail <EMAIL_1> then <EMAIL_2> then <EMAIL_1>"


def test_numbering_is_per_label():
    text = "a@x.io 555-1234"
    assert redact(text, [Span(0, 6, "EMAIL"), Span(7, 15, "PHONE")]) == "<EMAIL_1> <PHONE_1>"


def test_no_spans_returns_text_unchanged():
    assert redact("hello", []) == "hello"
