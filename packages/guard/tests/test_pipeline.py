from __future__ import annotations

from boundary_guard import Action, CheckContext, DecisionEvent, Guard, Mode, Stage
from guard_testkit import make_config, policy


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[DecisionEvent] = []

    async def record(self, event: DecisionEvent) -> None:
        self.events.append(event)


class BrokenSink:
    async def record(self, event: DecisionEvent) -> None:
        raise RuntimeError("sink down")


async def check(guard: Guard, text: str = "hello", stage: Stage = Stage.USER_INPUT, **ctx):
    return await guard.check(stage, text, CheckContext(**ctx))


async def test_no_policies_allows():
    result = await check(Guard(make_config()))
    assert result.action is Action.ALLOW
    assert result.decisions == []
    assert result.text == "hello"


async def test_strongest_enforced_action_wins():
    guard = Guard(
        make_config(
            policy("flagger", action="flag", triggered=True),
            policy("blocker", action="block", triggered=True),
            policy("escalator", action="escalate", triggered=True),
            policy("quiet", action="block", triggered=False),
        )
    )
    result = await check(guard)
    assert result.action is Action.BLOCK
    assert {d.policy_id: d.action for d in result.decisions} == {
        "flagger": Action.FLAG,
        "blocker": Action.BLOCK,
        "escalator": Action.ESCALATE,
        "quiet": Action.ALLOW,
    }


async def test_escalate_beats_redact():
    guard = Guard(
        make_config(
            policy("pii", action="redact", match="bob"),
            policy("review", action="escalate", triggered=True),
        )
    )
    result = await check(guard, "hi bob")
    assert result.action is Action.ESCALATE
    assert result.text == "hi <X_1>"


async def test_policies_only_run_for_their_stages():
    guard = Guard(make_config(policy("a", stages=["tool_output"], triggered=True)))
    assert (await check(guard, stage=Stage.USER_INPUT)).decisions == []
    assert (await check(guard, stage=Stage.TOOL_OUTPUT)).action is Action.BLOCK


async def test_shadow_records_would_action_but_changes_nothing():
    guard = Guard(
        make_config(
            policy("blocker", action="block", triggered=True, mode="shadow"),
            policy("pii", action="redact", match="secret", mode="shadow"),
        )
    )
    result = await check(guard, "my secret")
    assert result.action is Action.ALLOW
    assert result.would_action is Action.BLOCK
    assert result.text == "my secret"
    assert all(d.mode is Mode.SHADOW and d.action is Action.ALLOW for d in result.decisions)
    assert result.decision("pii").would_action is Action.REDACT


async def test_off_policy_is_skipped():
    guard = Guard(make_config(policy("a", triggered=True, mode="off")))
    result = await check(guard)
    assert result.decisions == []
    assert result.action is Action.ALLOW


async def test_detector_can_opt_out_via_applies():
    guard = Guard(make_config(policy("a", triggered=True, applies=False)))
    assert (await check(guard)).decisions == []


async def test_runtime_mode_override_changes_behaviour_and_hash():
    guard = Guard(make_config(policy("a", triggered=True)))
    original_hash = guard.config_hash

    guard.set_mode("a", Mode.SHADOW)
    assert guard.config_hash != original_hash
    result = await check(guard)
    assert result.action is Action.ALLOW
    assert result.would_action is Action.BLOCK
    assert result.config_hash == guard.config_hash

    guard.set_mode("a", None)
    assert guard.config_hash == original_hash
    assert (await check(guard)).action is Action.BLOCK


async def test_timeout_fails_closed_by_default():
    guard = Guard(make_config(policy("slow", action="flag", triggered=True, delay_ms=200, timeout_ms=20)))
    result = await check(guard)
    decision = result.decision("slow")
    assert decision.error == "timeout after 20ms"
    assert decision.action is Action.BLOCK
    assert result.latency_ms < 150


async def test_error_fails_open_when_configured():
    guard = Guard(make_config(policy("flaky", action="block", **{"raise": True}, on_error="fail_open")))
    result = await check(guard)
    assert result.action is Action.ALLOW
    assert result.decision("flaky").error == "RuntimeError: boom"


async def test_redaction_applies_enforced_spans_only():
    guard = Guard(
        make_config(
            policy("emails", action="redact", match="a@x.io", label="EMAIL"),
            policy("names", action="redact", match="Alice", label="NAME", mode="shadow"),
        )
    )
    result = await check(guard, "Alice wrote to a@x.io")
    assert result.action is Action.REDACT
    assert result.text == "Alice wrote to <EMAIL_1>"


async def test_redact_without_spans_blocks_instead():
    guard = Guard(make_config(policy("pii", action="redact", triggered=True)))
    result = await check(guard)
    assert result.action is Action.BLOCK
    assert "no spans" in result.decision("pii").reasons[-1]


async def test_transforms_run_first_and_detectors_see_rewrite():
    guard = Guard(
        make_config(
            policy("fix", type="stub_transform", action="block", rewrite="fixed text"),
            policy("scan", action="redact", match="fixed", label="W"),
        )
    )
    result = await check(guard, "broken text")
    assert result.decision("fix").rewritten is True
    assert result.text == "<W_1> text"


async def test_shadow_transform_does_not_rewrite():
    guard = Guard(
        make_config(policy("fix", type="stub_transform", action="block", rewrite="x", mode="shadow"))
    )
    result = await check(guard, "original")
    assert result.text == "original"
    assert result.decision("fix").rewritten is False


async def test_async_policy_reports_to_sinks_only():
    sink = RecordingSink()
    guard = Guard(
        make_config(
            policy(
                "judge",
                action="flag",
                stages=["final_output"],
                execution="async",
                triggered=True,
                delay_ms=10,
            ),
            policy("fast", action="allow", stages=["final_output"], triggered=False),
        ),
        sinks=[sink],
    )
    result = await check(guard, stage=Stage.FINAL_OUTPUT, run_id="r1")
    assert result.pending_async == ["judge"]
    assert [d.policy_id for d in result.decisions] == ["fast"]

    await guard.drain()
    sync_event, async_event = sink.events
    assert not sync_event.is_async
    assert async_event.is_async
    assert async_event.ctx.run_id == "r1"
    assert async_event.decisions[0].would_action is Action.FLAG


async def test_async_queue_overflow_is_counted():
    guard = Guard(
        make_config(policy("judge", action="flag", stages=["final_output"], execution="async", delay_ms=50)),
        max_pending_async=1,
    )
    await check(guard, stage=Stage.FINAL_OUTPUT)
    await check(guard, stage=Stage.FINAL_OUTPUT)
    assert guard.dropped_async == 1
    await guard.drain()


async def test_broken_sink_does_not_break_check():
    guard = Guard(make_config(policy("a", triggered=True)), sinks=[BrokenSink()])
    assert (await check(guard)).action is Action.BLOCK


async def test_decision_carries_metadata():
    guard = Guard(
        make_config(policy("a", triggered=True, score=0.9, threshold=0.5, cost_usd=0.001, reasons=["r"]))
    )
    result = await check(guard)
    d = result.decision("a")
    assert (d.score, d.threshold, d.cost_usd, d.reasons, d.detector) == (0.9, 0.5, 0.001, ["r"], "stub")
    assert d.policy_version == "1"
    assert result.cost_usd == 0.001
