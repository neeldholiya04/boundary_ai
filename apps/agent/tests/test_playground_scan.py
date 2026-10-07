from boundary_agent.guarding import GuardDecisionSink
from boundary_agent.playground import scan_text
from boundary_guard import CheckContext, DecisionEvent, Guard, Stage


def guard(tmp_path):
    (tmp_path / "rules.yaml").write_text(
        "name: rules\nrules:\n  - {id: code, label: CODE, pattern: 'SECRET-[0-9]+'}\n", encoding="utf-8"
    )
    (tmp_path / "guard.yaml").write_text(
        "version: 1\npolicies:\n"
        "  - id: codes\n    stages: [user_input]\n    detects: [secret]\n    action: redact\n"
        "    detector: {type: regex_rules, ruleset: rules.yaml}\n",
        encoding="utf-8",
    )
    return Guard.from_yaml(tmp_path / "guard.yaml")


async def test_scan_returns_per_policy_verdicts_spans_and_redacted_text(tmp_path):
    text = "my code is SECRET-123 ok"
    out = await scan_text(guard(tmp_path), Stage.USER_INPUT, text)
    assert out["action"] == "redact"
    assert "SECRET-123" not in out["text"]
    [policy] = out["policies"]
    assert policy["policy_id"] == "codes" and policy["action"] == "redact"
    [span] = policy["spans"]
    assert text[span["start"] : span["end"]] == "SECRET-123"  # offsets index the submitted text


async def test_decision_sink_never_stores_playground_events():
    class NoSession:
        def __call__(self):
            raise AssertionError("the sink must not open a DB session for playground events")

    sink = GuardDecisionSink(NoSession(), broker=None)
    event = DecisionEvent(Stage.USER_INPUT, CheckContext(metadata={"source": "playground"}), [], "h", True)
    await sink.record(event)  # returns without touching the database


async def test_decision_sink_skips_every_non_app_source():
    class NoSession:
        def __call__(self):
            raise AssertionError("no DB session for warm-up events")

    sink = GuardDecisionSink(NoSession(), broker=None)
    event = DecisionEvent(Stage.FINAL_OUTPUT, CheckContext(metadata={"source": "warmup"}), [], "h", True)
    await sink.record(event)
