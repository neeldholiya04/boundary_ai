from __future__ import annotations

from boundary_eval.tune import sweep, tune_markdown


def result(records, threshold=0.5):
    return {
        "policies": {"inj": {"detects": ["injection"], "detector": "hf_classifier", "threshold": threshold}},
        "records": records,
    }


def rec(split, score, attack):
    return {"split": split, "labels": ["injection"] if attack else [], "policies": {"inj": {"score": score}}}


RECORDS = [
    rec("dev", 0.95, True),
    rec("dev", 0.70, True),
    rec("dev", 0.40, True),
    rec("dev", 0.60, False),
    rec("dev", 0.10, False),
    rec("dev", 0.05, False),
    rec("dev", 0.02, False),
    rec("test", 0.80, True),
    rec("test", 0.65, False),
]


def test_recommendation_respects_the_fpr_budget():
    current, best = sweep(result(RECORDS), "inj", max_fpr=0.0)
    assert current.threshold == 0.5
    assert (current.dev.tp, current.dev.fp) == (2, 1)
    # 0.70 is the lowest threshold with no dev false alarm.
    assert best.threshold == 0.70
    assert (best.dev.tp, best.dev.fp) == (2, 0)
    assert (best.test.tp, best.test.fp) == (1, 0)


def test_looser_budget_allows_lower_threshold():
    _, best = sweep(result(RECORDS), "inj", max_fpr=0.25)
    assert best.threshold == 0.40
    assert best.dev.tp == 3


def test_no_dev_positives_means_no_recommendation():
    current, best = sweep(result([rec("dev", 0.1, False)]), "inj", max_fpr=0.05)
    assert current is None and best is None


def test_markdown_skips_regex_policies():
    r = result(RECORDS)
    r["policies"]["regex"] = {"detects": ["injection"], "detector": "regex_rules", "threshold": None}
    md = tune_markdown(r, 0.05)
    assert "`inj`" in md and "`regex`" not in md


def test_tail_recommendations_are_flagged():
    records = [
        rec("dev", 0.999, True),
        rec("dev", 0.990, True),
        rec("dev", 0.5, False),
        rec("dev", 0.1, False),
    ]
    md = tune_markdown(result(records), max_fpr=0.0)
    assert "score tail" in md
    assert "score tail" not in tune_markdown(result(RECORDS), max_fpr=0.0)
