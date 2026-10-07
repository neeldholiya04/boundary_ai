"""Prometheus metrics for any app using boundary_guard (optional extra: `boundary-guard[metrics]`).

Add `PrometheusSink()` to a Guard's sinks and every decision is counted. Sinks see both blocking
checks and background (async) ones, so the metrics cover all policies:

    guard_checks_total{source, stage, action}                  blocking checks by overall action
    guard_decisions_total{source, policy, stage, action, would_action, mode}
    guard_would_block_total{source, policy, stage}             would block if enforced (shadow included)
    guard_latency_seconds{source, policy, stage}               histogram
    guard_errors_total{source, policy, stage}                  detector errors and timeouts

`source` is `ctx.metadata["source"]` (default "app"), so an app can keep e.g. demo traffic apart
from real traffic. Labels hold ids, stages and actions only: never text, scores or span contents.
"""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram

from boundary_guard.core.pipeline import DecisionEvent
from boundary_guard.core.types import Action

# Guard checks range from sub-millisecond regexes to multi-second NLI on long pages.
LATENCY_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0)
_ORDER = [Action.ALLOW, Action.FLAG, Action.REDACT, Action.ESCALATE, Action.BLOCK]


@dataclass(slots=True)
class _Metrics:
    checks: Counter
    decisions: Counter
    would_block: Counter
    latency: Histogram
    errors: Counter


_by_registry: dict[int, _Metrics] = {}


def _metrics(registry: CollectorRegistry) -> _Metrics:
    """One set of collectors per registry: a second sink on the same registry reuses them rather
    than failing on duplicate registration."""
    found = _by_registry.get(id(registry))
    if found is not None:
        return found
    per_policy = ["source", "policy", "stage"]
    found = _Metrics(
        checks=Counter(
            "guard_checks_total",
            "Blocking guard checks by overall action.",
            ["source", "stage", "action"],
            registry=registry,
        ),
        decisions=Counter(
            "guard_decisions_total",
            "Policy decisions by applied action, would-be action and mode.",
            [*per_policy, "action", "would_action", "mode"],
            registry=registry,
        ),
        would_block=Counter(
            "guard_would_block_total",
            "Decisions that would block if the policy were enforced (shadow included).",
            per_policy,
            registry=registry,
        ),
        latency=Histogram(
            "guard_latency_seconds",
            "Policy check latency.",
            per_policy,
            buckets=LATENCY_BUCKETS,
            registry=registry,
        ),
        errors=Counter(
            "guard_errors_total", "Policy checks that errored or timed out.", per_policy, registry=registry
        ),
    )
    _by_registry[id(registry)] = found
    return found


class PrometheusSink:
    """A DecisionSink that records every guard decision as Prometheus metrics."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self.metrics = _metrics(registry)

    async def record(self, event: DecisionEvent) -> None:
        m = self.metrics
        stage = event.stage.value
        source = str(event.ctx.metadata.get("source") or "app")
        for d in event.decisions:
            m.decisions.labels(
                source, d.policy_id, stage, d.action.value, d.would_action.value, d.mode.value
            ).inc()
            if d.would_action is Action.BLOCK:
                m.would_block.labels(source, d.policy_id, stage).inc()
            m.latency.labels(source, d.policy_id, stage).observe(d.latency_ms / 1000)
            if d.error:
                m.errors.labels(source, d.policy_id, stage).inc()
        if not event.is_async:
            overall = max((d.action for d in event.decisions), key=_ORDER.index, default=Action.ALLOW)
            m.checks.labels(source, stage, overall.value).inc()
