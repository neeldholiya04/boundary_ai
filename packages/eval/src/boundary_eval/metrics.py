from __future__ import annotations

import math
from dataclasses import dataclass

Z_95 = 1.959963984540054


def wilson_interval(successes: int, trials: int, z: float = Z_95) -> tuple[float, float] | None:
    """95% Wilson score interval for a binomial proportion. None when there are no trials.

    Preferred over the normal approximation because our counts are small and rates are often
    0 or 1, where the normal interval collapses to zero width.
    """
    if trials == 0:
        return None
    p = successes / trials
    denom = 1 + z**2 / trials
    centre = (p + z**2 / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z**2 / (4 * trials**2)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (no interpolation, so it is always an observed value)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


@dataclass(slots=True)
class Confusion:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    def add(self, *, positive: bool, fired: bool) -> None:
        if positive and fired:
            self.tp += 1
        elif positive:
            self.fn += 1
        elif fired:
            self.fp += 1
        else:
            self.tn += 1

    @property
    def positives(self) -> int:
        return self.tp + self.fn

    @property
    def negatives(self) -> int:
        return self.fp + self.tn

    @property
    def catch_rate(self) -> float | None:
        return _ratio(self.tp, self.positives)

    @property
    def fpr(self) -> float | None:
        return _ratio(self.fp, self.negatives)

    @property
    def precision(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.catch_rate
        if p is None or r is None or p + r == 0:
            return None
        return 2 * p * r / (p + r)

    def summary(self) -> dict[str, object]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "tn": self.tn,
            "fn": self.fn,
            "positives": self.positives,
            "negatives": self.negatives,
            "catch_rate": self.catch_rate,
            "catch_rate_ci": wilson_interval(self.tp, self.positives),
            "fpr": self.fpr,
            "fpr_ci": wilson_interval(self.fp, self.negatives),
            "precision": self.precision,
            "f1": self.f1,
        }
