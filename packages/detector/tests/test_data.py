from __future__ import annotations

import random

from boundary_detector.data import Example, _content_hash, _splice, write_jsonl


def test_splice_inserts_injection_and_is_deterministic():
    carrier = "line one\nline two\nline three\nline four"
    a = _splice(carrier, "MARKER-X", random.Random(1))
    b = _splice(carrier, "MARKER-X", random.Random(1))
    assert a == b  # same seed -> same placement/wrapper
    assert "MARKER-X" in a
    # every original line survives (injection is added, not replacing content)
    for line in carrier.split("\n"):
        assert line in a


def test_splice_varies_with_seed():
    carrier = "\n".join(f"line {i}" for i in range(20))
    variants = {_splice(carrier, "MARKER-X", random.Random(s)) for s in range(8)}
    assert len(variants) > 1


def test_content_hash_ignores_whitespace_and_case():
    assert _content_hash("Ignore  Previous\nInstructions") == _content_hash("ignore previous instructions")


def test_write_jsonl_roundtrip(tmp_path):
    import json

    rows = [Example(id="a", text="hi", label=1, source="s", carrier_id="c", injection_id="i")]
    write_jsonl(rows, tmp_path / "d.jsonl")
    loaded = json.loads((tmp_path / "d.jsonl").read_text().strip())
    assert loaded["label"] == 1 and loaded["injection_id"] == "i"
