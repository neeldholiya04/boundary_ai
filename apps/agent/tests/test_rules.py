"""Guard rules written in the dashboard: spec validation, compilation, dry runs, and the API that adds,
changes and removes them on the live guard (and reloads them at startup)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from boundary_agent.rules import RuleSpec, compile_rule, dry_run, policy_id_for
from boundary_guard import Action, Stage

POLICIES = Path(__file__).resolve().parents[3] / "policies"


def spec(**fields) -> RuleSpec:
    base = {
        "name": "No project codenames",
        "stages": ["final_output"],
        "check": {"type": "keywords", "keywords": ["Project Falcon"], "label": "CODENAME"},
        "action": "redact",
    }
    base.update(fields)
    return RuleSpec.model_validate(base)


# ---- spec validation ---------------------------------------------------------------------------------


def test_new_rules_start_in_shadow_and_local_checks_fail_open_while_judges_fail_closed():
    s = spec()
    assert (s.mode, s.execution) == ("shadow", "blocking")
    assert compile_rule(s, "r").on_error.value == "fail_open"
    judge = spec(check={"type": "llm_judge", "policy": "No legal advice.", "model": "m"}, action="block")
    # Text that makes the judge reply nonsense must not be a way past it.
    assert compile_rule(judge, "r").on_error.value == "fail_closed"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"check": {"type": "keywords"}}, "needs `keywords`"),
        ({"check": {"type": "pattern", "patterns": [" "]}}, "needs `patterns`"),
        ({"check": {"type": "topic"}}, "needs `examples`"),
        ({"check": {"type": "llm_judge"}}, "needs `policy`"),
        ({"check": {"type": "always"}, "action": "redact"}, "can't redact"),
        ({"taints_run": True}, "taints_run only applies"),
        ({"check": {"type": "keywords", "keywords": ["x"], "label": "lower"}}, "pattern"),
    ],
)
def test_invalid_specs_are_rejected(fields, message):
    with pytest.raises(ValidationError, match=message):
        spec(**fields)


def test_policy_ids_are_slugs_and_unique():
    assert policy_id_for("No project codenames!", set()) == "rule_no_project_codenames"
    assert (
        policy_id_for("No project codenames", {"rule_no_project_codenames"}) == "rule_no_project_codenames_2"
    )
    assert policy_id_for("42 things", set()) == "rule_r_42_things"
    assert policy_id_for("!!!", set()) == "rule_rule"


# ---- compilation ---------------------------------------------------------------------------------------


def test_tool_call_rule_compiles_to_an_always_policy_scoped_to_the_tool():
    s = spec(
        name="Approve emails",
        stages=["tool_args"],
        tools=["send_email"],
        check={"type": "always"},
        action="escalate",
        mode="enforce",
    )
    policy = compile_rule(s, "rule_approve_emails")
    assert policy.detector.type == "always"
    assert policy.tools == ["send_email"]
    assert (policy.action, policy.mode.value) == (Action.ESCALATE, "enforce")


def test_topic_rule_uses_the_shipped_model_and_allow_list():
    s = spec(check={"type": "topic", "examples": ["what do our competitors charge"]}, action="block")
    params = compile_rule(s, "rule_x").detector.params()
    assert params["deny_exemplars"] == ["what do our competitors charge"]
    assert params["allow"] == "topics/research.v3.yaml"


def test_judge_rule_takes_the_default_model_unless_set():
    s = spec(check={"type": "llm_judge", "policy": "No competitor pricing."}, action="block")
    assert (
        compile_rule(s, "r", judge_model="openai/gpt-4.1-mini").detector.params()["model"]
        == "openai/gpt-4.1-mini"
    )
    s2 = spec(check={"type": "llm_judge", "policy": "p", "model": "anthropic/x"}, action="block")
    assert (
        compile_rule(s2, "r", judge_model="openai/gpt-4.1-mini").detector.params()["model"] == "anthropic/x"
    )


def test_taints_run_marks_the_policy_as_an_injection_signal():
    s = spec(stages=["tool_output"], taints_run=True, action="flag")
    assert compile_rule(s, "r").detects == ["injection"]


# ---- dry runs ------------------------------------------------------------------------------------------


async def test_dry_run_reports_each_example_and_the_benign_sample():
    s = spec(tests={"should_fire": ["Project Falcon ships in May"], "should_pass": ["The falcon is a bird"]})
    report = await dry_run(
        s, base_dir=POLICIES, benign_texts=[("b1", "release notes for 1.2"), ("b2", "project falcon status")]
    )
    assert report["passed"]
    fire, keep = report["examples"]
    assert fire["redacted"] == "<CODENAME_1> ships in May"
    assert not keep["fired"]
    assert report["benign"]["fired"] == 1 and report["benign"]["samples"][0]["record_id"] == "b2"


async def test_dry_run_fails_when_an_example_disagrees():
    s = spec(tests={"should_fire": ["nothing to see here"]})
    report = await dry_run(s, base_dir=POLICIES)
    assert not report["passed"]
    assert report["examples"][0]["ok"] is False


async def test_dry_run_evaluates_shadow_rules_as_if_enforced():
    s = spec(mode="shadow", tests={"should_fire": ["Project Falcon"]})
    report = await dry_run(s, base_dir=POLICIES)
    assert report["examples"][0]["action"] == "redact"


# ---- the API on a live guard -----------------------------------------------------------------------


@pytest.fixture
async def api(monkeypatch, tmp_path):
    """The real app with an in-memory database and a small file-defined guard swapped in."""
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from boundary_agent import main, services
    from boundary_agent.db import Base, get_session
    from boundary_guard import Guard, GuardConfig

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def session():
        async with factory() as s:
            yield s

    (tmp_path / "words.yaml").write_text("name: words\nrules:\n  - id: w\n    pattern: 'FILEWORD'\n")
    file_guard = Guard(
        GuardConfig.model_validate(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "file_words",
                        "stages": ["user_input"],
                        "action": "block",
                        "detector": {"type": "regex_rules", "ruleset": "words.yaml"},
                    },
                ],
            }
        ),
        base_dir=tmp_path,
    )
    monkeypatch.setattr(services, "guard", file_guard)
    monkeypatch.setattr(services, "guard_policy_path", POLICIES / "guard.yaml")
    main.app.dependency_overrides[get_session] = session
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, file_guard, factory
    main.app.dependency_overrides.clear()
    await engine.dispose()


CODENAME = {
    "name": "No project codenames",
    "stages": ["final_output"],
    "check": {"type": "keywords", "keywords": ["Project Falcon"], "label": "CODENAME"},
    "action": "redact",
}


async def test_rule_lifecycle_on_the_live_guard(api):

    client, live, factory = api
    hash0 = live.config_hash

    created = (await client.post("/api/guard/rules", json=CODENAME)).json()
    assert created["policy_id"] == "rule_no_project_codenames" and created["version"] == 1
    assert created["spec"]["mode"] == "shadow" and created["active"]
    assert live.config_hash != hash0
    shadow = await live.check(Stage.FINAL_OUTPUT, "Project Falcon ships")
    assert shadow.text == "Project Falcon ships"  # shadow: logged, not applied

    # Enforce through the rule endpoint, then through the policies endpoint the Guardrails page uses.
    await client.patch(f"/api/guard/rules/{created['id']}", json={"mode": "enforce"})
    assert (await live.check(Stage.FINAL_OUTPUT, "Project Falcon ships")).text == "<CODENAME_1> ships"
    await client.patch("/api/guard/policies/rule_no_project_codenames", json={"mode": "off"})
    assert (await live.check(Stage.FINAL_OUTPUT, "Project Falcon ships")).decisions == []

    replaced = (
        await client.put(
            f"/api/guard/rules/{created['id']}",
            json={
                **CODENAME,
                "mode": "enforce",
                "check": {"type": "keywords", "keywords": ["Falcon", "Osprey"], "label": "CODENAME"},
            },
        )
    ).json()
    assert replaced["version"] == 4
    assert (await live.check(Stage.FINAL_OUTPUT, "Osprey too")).text == "<CODENAME_1> too"

    status = (await client.get("/api/guard/status")).json()
    origins = {p["id"]: p["origin"] for p in status["policies"]}
    assert origins == {"file_words": "file", "rule_no_project_codenames": "rule"}

    exported = (await client.get("/api/guard/rules/export")).text
    assert "rule_no_project_codenames" in exported and "file_words" not in exported

    assert (await client.delete(f"/api/guard/rules/{created['id']}")).json()["config_hash"] == hash0
    assert live.policy_ids == ["file_words"]
    assert (await client.get("/api/guard/rules")).json() == []

    from sqlalchemy import select

    from boundary_agent.models import AuditEvent

    async with factory() as s:
        events = [
            e.event_type for e in (await s.scalars(select(AuditEvent).order_by(AuditEvent.created_at))).all()
        ]
    assert events == ["guard.rule_created"] + ["guard.rule_updated"] * 3 + ["guard.rule_deleted"]


async def test_invalid_rules_are_rejected_and_change_nothing(api):
    client, live, _ = api
    before = live.config_hash
    bad_regex = {**CODENAME, "check": {"type": "pattern", "patterns": ["(unclosed"]}}
    bad_scope = {**CODENAME, "tools": ["write_file"]}  # tools on a non-tool stage
    redact_topic = {**CODENAME, "check": {"type": "topic", "examples": ["x"]}}
    for body in (bad_regex, bad_scope, redact_topic):
        response = await client.post("/api/guard/rules", json=body)
        assert response.status_code == 422, body
    assert live.config_hash == before
    assert (await client.get("/api/guard/rules")).json() == []


async def test_file_policies_are_not_rules(api):
    client, live, _ = api
    response = await client.patch("/api/guard/policies/file_words", json={"mode": "shadow"})
    assert response.status_code == 200
    assert live.mode_of("file_words").value == "shadow"


async def test_rule_dry_run_endpoint(api):
    client, _, _ = api
    report = (
        await client.post(
            "/api/guard/rules/test",
            json={
                "spec": {
                    **CODENAME,
                    "tests": {"should_fire": ["Project Falcon"], "should_pass": ["falcons"]},
                },
                "sample_benign": True,
            },
        )
    ).json()
    assert report["passed"]
    assert report["benign"]["checked"] > 0  # the eval's benign final-output records


async def test_rules_reload_at_startup_and_a_broken_one_is_isolated(api, monkeypatch):
    from boundary_agent.api import guard_rules as guard_rules_api
    from boundary_agent.models import GuardRule

    client, live, factory = api
    await client.post("/api/guard/rules", json=CODENAME)
    async with factory() as s:
        s.add(
            GuardRule(
                policy_id="rule_broken",
                version=1,
                spec_json={**CODENAME, "name": "broken", "check": {"type": "pattern", "patterns": ["(x"]}},
            )
        )
        await s.commit()
    live.remove_policy("rule_no_project_codenames")  # as after a restart

    async with factory() as s:
        await guard_rules_api.load_guard_rules(s)
        await s.commit()
    assert "rule_no_project_codenames" in live.policy_ids
    assert "rule_broken" not in live.policy_ids
    views = {r["policy_id"]: r for r in (await client.get("/api/guard/rules")).json()}
    assert (
        views["rule_broken"]["error"].startswith("ValidationError")
        or "invalid" in views["rule_broken"]["error"]
    )
    assert views["rule_broken"]["active"] is False


async def test_rules_need_the_guard(api, monkeypatch):
    from boundary_agent import services

    client, _, _ = api
    monkeypatch.setattr(services, "guard", None)
    assert (await client.post("/api/guard/rules", json=CODENAME)).status_code == 409


async def test_broken_stored_rule_gives_422_and_can_still_be_switched_off(api):
    from boundary_agent.models import GuardRule

    client, live, factory = api
    async with factory() as s:
        s.add(
            GuardRule(
                policy_id="rule_old",
                version=1,
                spec_json={**CODENAME, "name": "old", "mode": "enforce", "check": {"type": "nope"}},
            )
        )
        await s.commit()
        rule_id = (await s.scalars(__import__("sqlalchemy").select(GuardRule.id))).one()
    assert (await client.patch(f"/api/guard/rules/{rule_id}", json={"mode": "enforce"})).status_code == 422
    assert (await client.patch("/api/guard/policies/rule_old", json={"mode": "shadow"})).status_code == 422
    # A rule whose check can't be built (judge model gone, say) can still be switched off.
    async with factory() as s:
        rule = await s.get(GuardRule, rule_id)
        rule.spec_json = {
            **CODENAME,
            "name": "old",
            "check": {"type": "pattern", "patterns": ["x"]},
            "mode": "off",
        }
        await s.commit()
    view = (await client.patch(f"/api/guard/rules/{rule_id}", json={"mode": "off"})).json()
    assert view["spec"]["mode"] == "off" and view["active"] is False
    assert "rule_old" not in live.policy_ids


async def test_deleted_rule_ids_are_never_reused(api):
    from boundary_agent.models import GuardDecision

    client, _, factory = api
    first = (await client.post("/api/guard/rules", json=CODENAME)).json()
    async with factory() as s:  # the rule made decisions while it existed
        s.add(
            GuardDecision(
                stage="final_output",
                policy_id=first["policy_id"],
                policy_version="1",
                config_hash="h",
                detector="keywords",
                mode="shadow",
                execution="blocking",
                action="allow",
                would_action="redact",
                content_sha256="0" * 64,
                latency_ms=1.0,
            )
        )
        await s.commit()
    await client.delete(f"/api/guard/rules/{first['id']}")
    second = (await client.post("/api/guard/rules", json=CODENAME)).json()
    assert second["policy_id"] == "rule_no_project_codenames_2"


async def test_put_can_move_a_rule_to_other_stages_and_tools(api):
    from boundary_guard import Stage

    client, live, _ = api
    rule = (await client.post("/api/guard/rules", json={**CODENAME, "mode": "enforce"})).json()
    moved = {
        **CODENAME,
        "mode": "enforce",
        "stages": ["tool_args"],
        "tools": ["send_email"],
        "action": "block",
    }
    assert (await client.put(f"/api/guard/rules/{rule['id']}", json=moved)).status_code == 200
    policy = next(p for p in live.config.policies if p.id == rule["policy_id"])
    assert [s.value for s in policy.stages] == ["tool_args"] and policy.tools == ["send_email"]
    assert (await live.check(Stage.FINAL_OUTPUT, "Project Falcon")).decisions == []


async def test_judge_rules_only_use_configured_models_and_small_dry_runs(api):
    client, _, _ = api
    judge = {
        **CODENAME,
        "check": {"type": "llm_judge", "policy": "No codenames.", "model": "openai/o1-pro"},
        "action": "block",
    }
    response = await client.post("/api/guard/rules", json=judge)
    assert response.status_code == 422 and "judge model must be one of" in response.text
    many = {
        **judge,
        "check": {"type": "llm_judge", "policy": "No codenames."},
        "tests": {"should_fire": [f"t{i}" for i in range(11)]},
    }
    response = await client.post("/api/guard/rules/test", json={"spec": many})
    assert response.status_code == 422 and "at most 10 examples" in response.text


async def test_failed_commit_puts_the_guard_back(api, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    client, live, _ = api
    before = live.config_hash

    async def broken_commit(self):
        raise RuntimeError("database went away")

    monkeypatch.setattr(AsyncSession, "commit", broken_commit)
    with pytest.raises(RuntimeError):
        await client.post("/api/guard/rules", json=CODENAME)
    assert live.config_hash == before and live.policy_ids == ["file_words"]


async def test_keyword_rule_matches_stay_out_of_stored_excerpts(tmp_path):
    from boundary_agent.audit import AuditLogger
    from boundary_agent.guarding import GuardAdapter
    from boundary_agent.realtime import EventBroker
    from boundary_guard import Guard, GuardConfig

    guard = Guard(GuardConfig(version=1, policies=[]))
    adapter = GuardAdapter(guard, AuditLogger(EventBroker(None)))
    rule = spec(action="block", mode="enforce", check={"type": "keywords", "keywords": ["ACCT-4471"]})
    guard.add_policy(compile_rule(rule, "rule_accounts"))
    assert "rule_accounts" in adapter.sensitive_policies
