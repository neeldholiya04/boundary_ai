"""Phase 9: runtime mode overrides and the shadow-vs-enforce stats view."""

from __future__ import annotations

from dataclasses import dataclass

from boundary_agent.guarding import aggregate_stats, apply_overrides
from boundary_guard import Guard, GuardConfig, Mode
from boundary_guard.core.detector import build_detector  # noqa: F401  (ensure registry import path)


def _guard() -> Guard:
    config = GuardConfig.model_validate(
        {
            "version": 1,
            "policies": [
                {
                    "id": "a",
                    "stages": ["user_input"],
                    "action": "block",
                    "mode": "enforce",
                    "detector": {"type": "stub", "triggered": True},
                },
                {
                    "id": "b",
                    "stages": ["final_output"],
                    "action": "flag",
                    "execution": "async",
                    "detector": {"type": "stub", "triggered": False},
                },
            ],
        }
    )
    return Guard(config)


@dataclass
class Row:
    policy_id: str
    would_action: str
    error: str | None = None
    latency_ms: float = 1.0


def test_apply_overrides_sets_modes_and_ignores_unknown():
    import guard_testkit  # noqa: F401  (registers the stub detector)

    guard = _guard()
    applied = apply_overrides(guard, [("a", "shadow"), ("ghost", "off")])
    assert applied == ["a"]
    assert guard.mode_of("a") is Mode.SHADOW


def test_aggregate_stats_counts_would_actions_and_latency():
    import guard_testkit  # noqa: F401

    guard = _guard()
    rows = [
        Row("a", "block", latency_ms=5),
        Row("a", "allow", latency_ms=1),
        Row("a", "block", error="timeout", latency_ms=3),
        Row("b", "flag", latency_ms=2),
    ]
    stats = {s["policy_id"]: s for s in aggregate_stats(guard, rows)}
    a = stats["a"]
    assert a["checks"] == 3 and a["would_fire"] == 2
    assert a["would_actions"] == {"block": 2}
    assert a["would_fire_rate"] == 2 / 3
    assert a["errors"] == 1
    assert a["p50_ms"] is not None
    b = stats["b"]
    assert b["would_fire"] == 1 and b["execution"] == "async"


def test_aggregate_stats_handles_no_traffic():
    import guard_testkit  # noqa: F401

    stats = {s["policy_id"]: s for s in aggregate_stats(_guard(), [])}
    assert stats["a"]["checks"] == 0
    assert stats["a"]["would_fire_rate"] is None and stats["a"]["p50_ms"] is None
