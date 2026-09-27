from __future__ import annotations

import copy
import json

import pytest

from boundary_eval.cli import main
from boundary_eval.compare import Gates, compare, comparison_markdown


def split(tp, fn, fp, tn, **extra):
    pos, neg = tp + fn, fp + tn
    return {
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "tn": tn,
        "positives": pos,
        "negatives": neg,
        "catch_rate": tp / pos if pos else None,
        "fpr": fp / neg if neg else None,
        "false_negatives": [],
        "false_positives": [],
        "by_category": {},
        **extra,
    }


def result(policies: dict, records: list | None = None, dataset="d1", config="c1") -> dict:
    return {
        "meta": {"dataset_hash": dataset, "config_hash": config},
        "policies": {
            pid: {"detects": ["injection"], "latency_ms": {"p99": 1.0}, "splits": {"test": s}}
            for pid, s in policies.items()
        },
        "records": records or [],
    }


GATES = Gates.model_validate(
    {
        "default": {"max_catch_rate_drop_pp": 2.0, "max_fpr_rise_pp": 2.0},
        "policies": {"inj": {"min_catch_rate": 0.5}},
    }
)


def test_unchanged_run_passes():
    base = result({"inj": split(8, 2, 1, 9)})
    cmp = compare(base, copy.deepcopy(base), GATES)
    assert cmp.passed
    assert cmp.policies[0].status == "ok"
    assert cmp.warnings == []


def test_catch_rate_drop_fails():
    cmp = compare(result({"inj": split(8, 2, 1, 9)}), result({"inj": split(7, 3, 1, 9)}), GATES)
    assert not cmp.passed
    assert "catch rate dropped 10.0pp" in cmp.failures[0]
    assert cmp.policies[0].status == "regressed"


def test_fpr_rise_fails():
    cmp = compare(result({"inj": split(8, 2, 1, 9)}), result({"inj": split(8, 2, 2, 8)}), GATES)
    assert any("FPR rose 10.0pp" in f for f in cmp.failures)


def test_improvement_passes_and_is_labelled():
    cmp = compare(result({"inj": split(8, 2, 1, 9)}), result({"inj": split(9, 1, 0, 10)}), GATES)
    assert cmp.passed
    assert cmp.policies[0].status == "improved"


def test_absolute_floor_applies_to_new_policies():
    cmp = compare(result({}), result({"inj": split(4, 6, 0, 10)}), GATES)
    assert not cmp.passed
    assert "below the floor" in cmp.failures[0]


def test_new_and_removed_policies():
    cmp = compare(result({"old": split(1, 0, 0, 1)}), result({"new": split(1, 0, 0, 1)}), GATES)
    statuses = {p.policy_id: p.status for p in cmp.policies}
    assert statuses == {"new": "new", "old": "removed"}
    assert cmp.passed


def test_dataset_and_config_changes_warn():
    base = result({"inj": split(8, 2, 1, 9)})
    cur = result({"inj": split(8, 2, 1, 9)}, dataset="d2", config="c2")
    warnings = compare(base, cur, GATES).warnings
    assert any("dataset changed" in w for w in warnings)
    assert any("policy config changed" in w for w in warnings)


def test_record_level_flips():
    def r(id, labels, fired):
        return {"id": id, "labels": labels, "policies": {"inj": {"fired": fired}}}

    base = result(
        {"inj": split(1, 1, 1, 1)},
        [r("a", ["injection"], True), r("b", ["injection"], False), r("c", [], True), r("d", [], False)],
    )
    cur = result(
        {"inj": split(1, 1, 1, 1)},
        [r("a", ["injection"], False), r("b", ["injection"], True), r("c", [], False), r("d", [], True)],
    )
    p = compare(base, cur, GATES).policies[0]
    assert (p.newly_missed, p.newly_caught, p.new_false_alarms, p.resolved_false_alarms) == (
        ["a"],
        ["b"],
        ["d"],
        ["c"],
    )


def test_markdown_mentions_failures_and_flips():
    base = result({"inj": split(8, 2, 1, 9)})
    md = comparison_markdown(compare(base, result({"inj": split(4, 6, 1, 9)}), GATES))
    assert "Eval gate failed" in md
    assert "below the floor" in md


def test_gates_for_policy_merges_default():
    gate = GATES.for_policy("inj")
    assert (gate.max_catch_rate_drop_pp, gate.min_catch_rate) == (2.0, 0.5)
    assert GATES.for_policy("other").min_catch_rate is None


@pytest.mark.parametrize(("current_tp", "code"), [(8, 0), (5, 1)])
def test_cli_compare_exit_code(tmp_path, current_tp, code):
    gates = tmp_path / "gates.yaml"
    gates.write_text("default: {max_catch_rate_drop_pp: 2.0}\n", encoding="utf-8")
    base, cur = tmp_path / "base.json", tmp_path / "cur.json"
    base.write_text(json.dumps(result({"inj": split(8, 2, 1, 9)})), encoding="utf-8")
    cur.write_text(json.dumps(result({"inj": split(current_tp, 10 - current_tp, 1, 9)})), encoding="utf-8")
    assert main(["compare", str(base), str(cur), "--gates", str(gates)]) == code


def test_cli_detectors_refuses_invalid_dataset(tmp_path, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "x"}\n', encoding="utf-8")
    assert main(["detectors", str(bad), "--quiet"]) == 2
    assert "dataset is invalid" in capsys.readouterr().err


def _e2e_result(asr, benign, config="filters_taint", split="test"):
    return {"configs": {config: {split: {"attack_success_rate": asr, "benign_task_success": benign}}}}


def test_e2e_gate_passes_within_thresholds():
    from boundary_eval.compare import E2EGate, check_e2e_gate

    gate = E2EGate(max_asr=0.15, min_benign_task_success=0.8)
    assert check_e2e_gate(_e2e_result(0.10, 0.9), gate) == []


def test_e2e_gate_flags_high_asr_and_low_utility():
    from boundary_eval.compare import E2EGate, check_e2e_gate

    gate = E2EGate(max_asr=0.15, min_benign_task_success=0.8)
    failures = check_e2e_gate(_e2e_result(0.40, 0.5), gate)
    assert any("attack success rate" in f for f in failures)
    assert any("benign task success" in f for f in failures)


def test_e2e_gate_is_silent_when_config_absent():
    from boundary_eval.compare import E2EGate, check_e2e_gate

    gate = E2EGate(config="filters_taint", max_asr=0.15)
    assert check_e2e_gate(_e2e_result(0.9, 0.1, config="no_defense"), gate) == []


def test_gates_yaml_has_e2e_section():
    from pathlib import Path

    from boundary_eval.compare import load_gates

    gates = load_gates(Path(__file__).resolve().parents[3] / "packages" / "eval" / "gates.yaml")
    assert gates.e2e.config == "filters_taint"
    assert gates.e2e.max_asr is not None
