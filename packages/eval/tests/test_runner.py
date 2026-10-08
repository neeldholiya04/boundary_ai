from __future__ import annotations

import json
from pathlib import Path

from boundary_eval.dataset import load_records
from boundary_eval.runner import dataset_hash, run_detectors, to_result
from boundary_guard import Guard, Mode
from guard_testkit import make_config, policy


def rec(id, split, stage, category, labels, text, **extra):
    return {
        "id": id,
        "split": split,
        "source": "t",
        "stage": stage,
        "category": category,
        "labels": labels,
        "text": text,
        **extra,
    }


def dataset(tmp_path: Path, *records: dict) -> list:
    path = tmp_path / "d.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    report = load_records([path])
    assert report.ok, report.errors
    return report.records


RECORDS = [
    rec("a1", "test", "user_input", "direct_injection", ["injection"], "BAD one"),
    rec("a2", "test", "user_input", "direct_injection", ["injection"], "subtle one"),
    rec("b1", "test", "user_input", "benign", [], "BAD word in a benign text"),
    rec("b2", "dev", "user_input", "benign", [], "fine"),
    rec("t1", "test", "tool_output", "indirect_injection", ["injection"], "BAD page"),
]


def detects(p: dict, labels: list[str]) -> dict:
    p["detects"] = labels
    return p


async def test_scores_on_would_action_per_split(tmp_path):
    guard = Guard(make_config(detects(policy("inj", match="BAD", mode="shadow"), ["injection"])))
    run = await run_detectors(guard, dataset(tmp_path, *RECORDS))
    result = to_result(run)
    test = result["policies"]["inj"]["splits"]["test"]
    # Only user_input records are scored (the policy's stage); t1 is at tool_output.
    assert (test["tp"], test["fn"], test["fp"], test["tn"]) == (1, 1, 1, 0)
    assert test["false_negatives"] == ["a2"]
    assert test["false_positives"] == ["b1"]
    assert result["policies"]["inj"]["mode"] == "shadow"
    assert result["policies"]["inj"]["splits"]["dev"]["negatives"] == 1


async def test_off_policies_are_measured_then_restored(tmp_path):
    guard = Guard(make_config(detects(policy("inj", match="BAD", mode="off"), ["injection"])))
    original_hash = guard.config_hash
    result = to_result(await run_detectors(guard, dataset(tmp_path, *RECORDS)))
    assert result["policies"]["inj"]["splits"]["test"]["tp"] == 1
    assert result["policies"]["inj"]["mode"] == "off"
    assert guard.mode_of("inj") is Mode.OFF
    assert guard.config_hash == original_hash


async def test_async_policies_are_collected(tmp_path):
    p = detects(
        policy("judge", action="flag", stages=["final_output"], execution="async", match="made up"),
        ["hallucination"],
    )
    guard = Guard(make_config(p))
    records = dataset(
        tmp_path,
        rec("h1", "test", "final_output", "hallucination", ["hallucination"], "a made up claim"),
        rec("h2", "test", "final_output", "benign", [], "a faithful summary"),
    )
    result = to_result(await run_detectors(guard, records))
    test = result["policies"]["judge"]["splits"]["test"]
    assert (test["tp"], test["tn"]) == (1, 1)
    assert result["policies"]["judge"]["execution"] == "async"


async def test_unscored_policies_still_report_latency(tmp_path):
    guard = Guard(make_config(policy("noop", triggered=False)))
    result = to_result(await run_detectors(guard, dataset(tmp_path, *RECORDS)))
    assert result["policies"]["noop"]["splits"] == {}
    assert result["policies"]["noop"]["latency_ms"]["n"] == 4


async def test_repeats_add_warmup_and_timed_passes(tmp_path):
    guard = Guard(make_config(detects(policy("inj", match="BAD"), ["injection"])))
    run = await run_detectors(guard, dataset(tmp_path, *RECORDS), repeats=3)
    assert len(run.policy_latencies["inj"]) == 4 * 3  # 4 user_input records × 3 timed passes
    assert to_result(run)["policies"]["inj"]["splits"]["test"]["tp"] == 1


async def test_errors_are_counted(tmp_path):
    guard = Guard(make_config(detects(policy("flaky", **{"raise": True}), ["injection"])))
    result = to_result(await run_detectors(guard, dataset(tmp_path, *RECORDS)))
    assert result["policies"]["flaky"]["errors"] == 4


def test_dataset_hash_changes_with_labels(tmp_path):
    a = dataset(tmp_path, rec("x", "test", "user_input", "benign", [], "hi"))
    b = dataset(tmp_path, rec("x", "test", "user_input", "off_topic", ["off_topic"], "hi"))
    assert dataset_hash(a) != dataset_hash(b)


async def test_result_records_are_serialisable(tmp_path):
    guard = Guard(make_config(detects(policy("inj", match="BAD"), ["injection"])))
    result = to_result(await run_detectors(guard, dataset(tmp_path, *RECORDS)))
    json.dumps(result)
    assert {r["id"] for r in result["records"]} == {"a1", "a2", "b1", "b2", "t1"}


async def test_timeouts_are_lifted_by_default_and_restored(tmp_path):
    slow = detects(policy("slow", match="BAD", delay_ms=50, timeout_ms=5), ["injection"])
    records = dataset(tmp_path, *RECORDS)

    guard = Guard(make_config(slow))
    result = to_result(await run_detectors(guard, records))
    assert result["policies"]["slow"]["errors"] == 0
    assert result["policies"]["slow"]["splits"]["test"]["tp"] == 1
    assert result["meta"]["timeouts"] == "lifted"
    assert guard.config.policies[0].timeout_ms == 5

    enforced = to_result(await run_detectors(Guard(make_config(slow)), records, enforce_timeouts=True))
    assert enforced["policies"]["slow"]["errors"] == 4
    assert enforced["meta"]["timeouts"] == "enforced"


def test_eval_guard_never_loads_known_secrets(tmp_path, monkeypatch):
    # Evals measure rules every machine shares; this machine's own keys would make verdicts (and the
    # playground's attack demo, which runs through here) depend on who runs them.
    from boundary_eval.runner import eval_guard

    (tmp_path / "rules.yaml").write_text("name: r\nrules:\n  - id: x\n    pattern: 'NEVER'\n")
    (tmp_path / "guard.yaml").write_text(
        "version: 1\npolicies:\n  - id: s\n    stages: [final_output]\n    action: redact\n"
        "    detector: {type: regex_rules, ruleset: rules.yaml, known_secrets_env: ['*_API_KEY']}\n"
    )
    monkeypatch.setenv("VENDOR_API_KEY", "a3f9c2e81b7d4f60a95e3c1d8b72f40e6a9c")
    guard = eval_guard(tmp_path / "guard.yaml")
    assert "known_secrets_env" not in guard.config.policies[0].detector.params()
