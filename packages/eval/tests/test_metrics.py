from __future__ import annotations

import pytest

from boundary_eval.metrics import Confusion, percentile, wilson_interval


@pytest.mark.parametrize(
    ("k", "n", "lo", "hi"),
    [
        (0, 10, 0.0, 0.2775),
        (5, 10, 0.2366, 0.7634),
        (10, 10, 0.7225, 1.0),
        (1, 1, 0.2065, 1.0),
    ],
)
def test_wilson_matches_reference_values(k, n, lo, hi):
    got_lo, got_hi = wilson_interval(k, n)
    assert got_lo == pytest.approx(lo, abs=1e-4)
    assert got_hi == pytest.approx(hi, abs=1e-4)


def test_wilson_without_trials_is_none():
    assert wilson_interval(0, 0) is None


def test_percentile_nearest_rank():
    values = [5.0, 1.0, 3.0, 2.0, 4.0]
    assert percentile(values, 50) == 3.0
    assert percentile(values, 99) == 5.0
    assert percentile(values, 0) == 1.0
    assert percentile([], 50) is None


def test_confusion_rates():
    c = Confusion()
    for positive, fired in [(True, True), (True, True), (True, False), (False, True), (False, False)]:
        c.add(positive=positive, fired=fired)
    assert (c.tp, c.fn, c.fp, c.tn) == (2, 1, 1, 1)
    assert c.catch_rate == pytest.approx(2 / 3)
    assert c.fpr == pytest.approx(0.5)
    assert c.precision == pytest.approx(2 / 3)
    assert c.f1 == pytest.approx(2 / 3)


def test_empty_confusion_has_no_rates():
    c = Confusion()
    assert c.catch_rate is None and c.fpr is None and c.precision is None and c.f1 is None
    assert c.summary()["catch_rate_ci"] is None
