"""Pick thresholds on the dev split from a detector-run result.

Uses the per-record scores stored in a `boundary-eval detectors` JSON, so no models are rerun.
For each scored policy, every observed score is a candidate threshold ("fires when score >=
threshold"). The recommendation is the threshold with the highest dev catch rate whose dev FPR
stays at or under `max_fpr`; ties go to the higher threshold (fewer false alarms). Test-split
numbers at that threshold are printed for information only: choosing on them would leak the
test set into the operating point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boundary_eval.metrics import Confusion, wilson_interval
from boundary_eval.report import ci, pct

# Thresholds outside this band sit in the tails of a probability output, where a few benign
# records decide the operating point. Lesson from v3 (docs/EVAL.md "Operating points").
TAIL_LOW, TAIL_HIGH = 0.02, 0.98


@dataclass(slots=True)
class Operating:
    threshold: float
    dev: Confusion
    test: Confusion


def _points(result: dict[str, Any], policy_id: str, split: str) -> list[tuple[float, bool]]:
    detects = set(result["policies"][policy_id]["detects"])
    out = []
    for rec in result["records"]:
        entry = rec["policies"].get(policy_id)
        if rec["split"] != split or entry is None or entry["score"] is None:
            continue
        if detects.intersection(rec.get("unlabeled", [])):
            continue
        out.append((float(entry["score"]), bool(detects.intersection(rec["labels"]))))
    return out


def _confusion(points: list[tuple[float, bool]], threshold: float) -> Confusion:
    c = Confusion()
    for score, positive in points:
        c.add(positive=positive, fired=score >= threshold)
    return c


def sweep(
    result: dict[str, Any], policy_id: str, max_fpr: float
) -> tuple[Operating | None, Operating | None]:
    """Returns (current operating point, recommended operating point)."""
    dev = _points(result, policy_id, "dev")
    test = _points(result, policy_id, "test")
    if not dev or not any(p for _, p in dev):
        return None, None

    current_threshold = result["policies"][policy_id].get("threshold")
    current = None
    if current_threshold is not None:
        current = Operating(
            current_threshold, _confusion(dev, current_threshold), _confusion(test, current_threshold)
        )

    best: Operating | None = None
    for threshold in sorted({s for s, _ in dev}, reverse=True):
        d = _confusion(dev, threshold)
        if (d.fpr or 0.0) > max_fpr:
            continue
        if best is None or (d.catch_rate or 0.0) > (best.dev.catch_rate or 0.0):
            best = Operating(threshold, d, _confusion(test, threshold))
    return current, best


def _row(label: str, op: Operating | None) -> str:
    if op is None:
        return f"| {label} | – | – | – | – | – |"
    d, t = op.dev, op.test
    return (
        f"| {label} | {op.threshold:.4g} "
        f"| {pct(d.catch_rate)}{ci(wilson_interval(d.tp, d.positives))} "
        f"| {pct(d.fpr)}{ci(wilson_interval(d.fp, d.negatives))} "
        f"| {pct(t.catch_rate)} | {pct(t.fpr)} |"
    )


def tune_markdown(result: dict[str, Any], max_fpr: float, policy_ids: list[str] | None = None) -> str:
    lines = [
        f"# Threshold sweep (dev split, target FPR ≤ {max_fpr:.0%})",
        "",
        "Test columns are for information only; the choice is made on dev.",
    ]
    for pid, policy in result["policies"].items():
        if policy_ids and pid not in policy_ids:
            continue
        # Not one score against one threshold: `all_of` combines two models (and a decide-alone band),
        # `embeddings_topic` has a similarity floor and a word minimum besides its margin. A sweep of
        # the reported score would recommend thresholds the policy doesn't use.
        if not policy["detects"] or policy["detector"] in (
            "regex_rules",
            "json_schema",
            "all_of",
            "embeddings_topic",
        ):
            continue
        current, best = sweep(result, pid, max_fpr)
        if current is None and best is None:
            continue
        lines += [
            "",
            f"## `{pid}`",
            "",
            "| | Threshold | Dev catch | Dev FPR | Test catch | Test FPR |",
            "|---|---|---|---|---|---|",
            _row("current", current),
            _row("recommended", best),
        ]
        if best is None:
            lines.append(f"\nNo threshold keeps dev FPR ≤ {max_fpr:.0%}.")
        elif not TAIL_LOW <= best.threshold <= TAIL_HIGH:
            lines.append(
                f"\n⚠ Recommended threshold {best.threshold:.4g} is in the score tail "
                f"(outside [{TAIL_LOW}, {TAIL_HIGH}]). Classifier scores are usually bimodal there, so the "
                "dev FPR tends not to transfer; check it against hard negatives before adopting it."
            )
    return "\n".join(lines) + "\n"
