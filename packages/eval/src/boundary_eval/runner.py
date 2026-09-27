from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from boundary_eval.dataset import LoadedRecord, Split
from boundary_eval.metrics import Confusion, percentile
from boundary_guard import Action, DecisionEvent, Guard, Mode, PolicyDecision, Stage

SCHEMA_VERSION = 1
_RECORD_KEY = "eval_record_id"
_NO_TIMEOUT_MS = 10 * 60 * 1000  # effectively unlimited for a single check


@dataclass(slots=True)
class PolicyOutcome:
    policy_id: str
    fired: bool
    would_action: Action
    score: float | None
    latency_ms: float
    cost_usd: float
    error: str | None
    is_async: bool


@dataclass(slots=True)
class RecordOutcome:
    loaded: LoadedRecord
    outcomes: dict[str, PolicyOutcome] = field(default_factory=dict)


@dataclass(slots=True)
class DetectorRun:
    guard: Guard
    records: list[RecordOutcome]
    # Timing samples across all passes (the first pass included when repeats == 1).
    policy_latencies: dict[str, list[float]]
    stage_latencies: dict[Stage, list[float]]
    repeats: int
    suite: str
    # Modes as configured, before `off` policies were switched to shadow for measurement.
    configured_modes: dict[str, Mode] = field(default_factory=dict)
    enforce_timeouts: bool = False


class _AsyncCollector:
    """Captures decisions from async policies, keyed by the eval record they belong to."""

    def __init__(self) -> None:
        self.by_record: dict[str, list[PolicyDecision]] = defaultdict(list)

    async def record(self, event: DecisionEvent) -> None:
        if event.is_async:
            record_id = event.ctx.metadata.get(_RECORD_KEY)
            if record_id is not None:
                self.by_record[record_id].extend(event.decisions)


def _outcome(decision: PolicyDecision, *, is_async: bool) -> PolicyOutcome:
    return PolicyOutcome(
        policy_id=decision.policy_id,
        fired=decision.would_action is not Action.ALLOW,
        would_action=decision.would_action,
        score=decision.score,
        latency_ms=decision.latency_ms,
        cost_usd=decision.cost_usd,
        error=decision.error,
        is_async=is_async,
    )


async def run_detectors(
    guard: Guard,
    records: list[LoadedRecord],
    *,
    repeats: int = 1,
    suite: str = "custom",
    enforce_timeouts: bool = False,
) -> DetectorRun:
    """Run every policy over every record and collect per-policy outcomes.

    Policies are scored on `would_action`, so shadow-mode policies are measured like enforced
    ones; `off` policies are switched to shadow for the run so they are measured too.
    With repeats > 1, one untimed warm-up pass runs first and `repeats` timed passes follow;
    outcomes come from the first timed pass (detectors are deterministic).

    By default policy timeouts are lifted for the run: a slow CI machine must not turn into
    fail-closed "detections" and make the gate flaky. Detection quality is judged on verdicts;
    timeout behaviour belongs to latency measurement (`enforce_timeouts=True`, load tests).
    The config hash is unaffected: it still identifies the production configuration.
    """
    original_timeouts = {p.id: p.timeout_ms for p in guard.config.policies}
    if not enforce_timeouts:
        for policy in guard.config.policies:
            policy.timeout_ms = _NO_TIMEOUT_MS
    configured_modes = {pid: guard.mode_of(pid) for pid in guard.policy_ids}
    for policy_id, mode in configured_modes.items():
        if mode is Mode.OFF:
            guard.set_mode(policy_id, Mode.SHADOW)
    collector = _AsyncCollector()
    guard.sinks.append(collector)

    policy_latencies: dict[str, list[float]] = defaultdict(list)
    stage_latencies: dict[Stage, list[float]] = defaultdict(list)
    outcomes = [RecordOutcome(r) for r in records]

    passes = repeats + (1 if repeats > 1 else 0)
    for pass_index in range(passes):
        timed = repeats == 1 or pass_index > 0
        first_timed = pass_index == (0 if repeats == 1 else 1)
        for outcome in outcomes:
            loaded = outcome.loaded
            ctx = loaded.record.check_context()
            ctx.metadata = {**ctx.metadata, _RECORD_KEY: loaded.record.id}
            collector.by_record.pop(loaded.record.id, None)

            result = await guard.check(loaded.record.stage, loaded.text, ctx)
            await guard.drain()

            if timed:
                stage_latencies[loaded.record.stage].append(result.latency_ms)
                for d in result.decisions:
                    policy_latencies[d.policy_id].append(d.latency_ms)
                for d in collector.by_record.get(loaded.record.id, []):
                    policy_latencies[d.policy_id].append(d.latency_ms)
            if first_timed:
                for d in result.decisions:
                    outcome.outcomes[d.policy_id] = _outcome(d, is_async=False)
                for d in collector.by_record.get(loaded.record.id, []):
                    outcome.outcomes[d.policy_id] = _outcome(d, is_async=True)

    guard.sinks.remove(collector)
    for policy in guard.config.policies:
        policy.timeout_ms = original_timeouts[policy.id]
    for policy_id, mode in configured_modes.items():
        guard.set_mode(policy_id, mode)  # clears the override when it matches the YAML
    return DetectorRun(
        guard,
        outcomes,
        dict(policy_latencies),
        dict(stage_latencies),
        repeats,
        suite,
        configured_modes,
        enforce_timeouts,
    )


# ---- scoring & serialisation ------------------------------------------------------------


def _latency_summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
    }


def dataset_hash(records: list[LoadedRecord]) -> str:
    rows = sorted(
        (
            r.record.id,
            r.record.split.value,
            r.record.stage.value,
            r.record.category.value,
            ",".join(sorted(label.value for label in r.record.labels)),
            r.content_hash,
            json.dumps(r.record.context, sort_keys=True, default=str),
        )
        for r in records
    )
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


def _git_state() -> dict[str, object]:
    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", *args], capture_output=True, text=True, check=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip()

    sha = git("rev-parse", "--short=12", "HEAD")
    status = git("status", "--porcelain")
    return {"sha": sha or "uncommitted", "dirty": bool(status) if status is not None else None}


def _score_policy(
    run: DetectorRun, policy_id: str, detects: set[str], stages: set[Stage]
) -> dict[str, object]:
    confusions: dict[str, Confusion] = defaultdict(Confusion)
    misses: dict[str, list[str]] = defaultdict(list)
    false_alarms: dict[str, list[str]] = defaultdict(list)
    by_source: dict[str, dict[str, Confusion]] = defaultdict(lambda: defaultdict(Confusion))
    by_category: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"n": 0, "fired": 0})
    )
    errors = 0
    costs: list[float] = []

    for outcome in run.records:
        record = outcome.loaded.record
        if record.stage not in stages:
            continue
        result = outcome.outcomes.get(policy_id)
        if result is None:  # detector opted out (e.g. schema check without a requested schema)
            continue
        costs.append(result.cost_usd)
        errors += result.error is not None
        if not detects or not record.scored_by(detects):
            continue
        positive = any(label.value in detects for label in record.labels)
        for split in (record.split.value, "all"):
            confusions[split].add(positive=positive, fired=result.fired)
            by_source[split][record.source].add(positive=positive, fired=result.fired)
            cell = by_category[split][record.category.value]
            cell["n"] += 1
            cell["fired"] += result.fired
            if positive and not result.fired:
                misses[split].append(record.id)
            elif not positive and result.fired:
                false_alarms[split].append(record.id)

    splits = {}
    for split in [s.value for s in Split] + ["all"]:
        if split not in confusions:
            continue
        splits[split] = {
            **confusions[split].summary(),
            "false_negatives": sorted(misses[split]),
            "false_positives": sorted(false_alarms[split]),
            "by_category": {k: dict(v) for k, v in sorted(by_category[split].items())},
            "by_source": {
                k: {"tp": c.tp, "fn": c.fn, "fp": c.fp, "tn": c.tn, "catch_rate": c.catch_rate, "fpr": c.fpr}
                for k, c in sorted(by_source[split].items())
            },
        }
    return {
        "splits": splits,
        "latency_ms": _latency_summary(run.policy_latencies.get(policy_id, [])),
        "cost_usd_per_1k": (sum(costs) / len(costs) * 1000) if costs else 0.0,
        "errors": errors,
        "checks": len(costs),
    }


def to_result(run: DetectorRun) -> dict[str, object]:
    guard = run.guard
    loaded = [o.loaded for o in run.records]
    policies = {}
    for policy in guard.config.policies:
        policies[policy.id] = {
            "detects": list(policy.detects),
            "stages": [s.value for s in policy.stages],
            "mode": run.configured_modes.get(policy.id, guard.mode_of(policy.id)).value,
            "execution": policy.execution.value,
            "action": policy.action.value,
            "detector": policy.detector.type,
            "threshold": guard.threshold_of(policy.id),
            **_score_policy(run, policy.id, set(policy.detects), set(policy.stages)),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "suite": run.suite,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "config_hash": guard.config_hash,
            "policy_version": guard.config.version,
            "dataset_hash": dataset_hash(loaded),
            "records": len(loaded),
            "repeats": run.repeats,
            "timeouts": "enforced" if run.enforce_timeouts else "lifted",
            "git": _git_state(),
            "python": sys.version.split()[0],
            "platform": platform.platform(terse=True),
        },
        "policies": policies,
        "stages": {
            stage.value: {"latency_ms": _latency_summary(values)}
            for stage, values in sorted(run.stage_latencies.items(), key=lambda kv: kv[0].value)
        },
        "records": [
            {
                "id": o.loaded.record.id,
                "split": o.loaded.record.split.value,
                "stage": o.loaded.record.stage.value,
                "category": o.loaded.record.category.value,
                "labels": [label.value for label in o.loaded.record.labels],
                "unlabeled": [label.value for label in o.loaded.record.unlabeled],
                "policies": {
                    pid: {
                        "fired": p.fired,
                        "would_action": p.would_action.value,
                        "score": p.score,
                        "error": p.error,
                    }
                    for pid, p in sorted(o.outcomes.items())
                },
            }
            for o in run.records
        ],
    }


def write_json(result: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=False) + "\n", encoding="utf-8")
