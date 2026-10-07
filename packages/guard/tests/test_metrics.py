from prometheus_client import CollectorRegistry

from boundary_guard import Action, CheckContext, DecisionEvent, Execution, Mode, PolicyDecision, Stage
from boundary_guard.metrics import PrometheusSink

APP = {"source": "app", "stage": "tool_output"}


def decision(policy, action, would, mode=Mode.ENFORCE, latency_ms=12.0, error=None):
    return PolicyDecision(
        policy_id=policy,
        policy_version="1",
        stage=Stage.TOOL_OUTPUT,
        mode=mode,
        execution=Execution.BLOCKING,
        action=action,
        would_action=would,
        detector="regex_rules",
        latency_ms=latency_ms,
        error=error,
    )


def event(decisions, *, is_async=False, source=None):
    ctx = CheckContext(metadata={"source": source} if source else {})
    return DecisionEvent(Stage.TOOL_OUTPUT, ctx, decisions, "abc", is_async)


async def test_sink_counts_decisions_shadow_would_blocks_latency_and_errors():
    registry = CollectorRegistry()
    await PrometheusSink(registry).record(
        event(
            [
                decision("secrets", Action.BLOCK, Action.BLOCK),
                decision("injection", Action.ALLOW, Action.BLOCK, mode=Mode.SHADOW, latency_ms=2500),
                decision("pii", Action.ALLOW, Action.ALLOW, error="timeout after 400ms"),
            ]
        )
    )
    get = registry.get_sample_value
    shadow = {**APP, "policy": "injection", "action": "allow", "would_action": "block", "mode": "shadow"}
    assert get("guard_decisions_total", shadow) == 1
    assert get("guard_would_block_total", {**APP, "policy": "injection"}) == 1
    assert get("guard_would_block_total", {**APP, "policy": "secrets"}) == 1
    assert get("guard_checks_total", {**APP, "action": "block"}) == 1
    assert get("guard_errors_total", {**APP, "policy": "pii"}) == 1
    assert get("guard_latency_seconds_bucket", {**APP, "policy": "injection", "le": "2.5"}) == 1


async def test_async_events_count_decisions_but_not_checks():
    registry = CollectorRegistry()
    await PrometheusSink(registry).record(event([decision("x", Action.FLAG, Action.FLAG)], is_async=True))
    get = registry.get_sample_value
    assert get("guard_checks_total", {**APP, "action": "flag"}) is None
    flagged = {**APP, "policy": "x", "action": "flag", "would_action": "flag", "mode": "enforce"}
    assert get("guard_decisions_total", flagged) == 1


async def test_source_label_separates_demo_traffic():
    registry = CollectorRegistry()
    await PrometheusSink(registry).record(
        event([decision("x", Action.BLOCK, Action.BLOCK)], source="playground")
    )
    get = registry.get_sample_value
    assert get("guard_checks_total", {**APP, "source": "playground", "action": "block"}) == 1
    assert get("guard_checks_total", {**APP, "action": "block"}) is None


def test_two_sinks_share_one_registry_without_duplicate_errors():
    registry = CollectorRegistry()
    assert PrometheusSink(registry).metrics is PrometheusSink(registry).metrics
