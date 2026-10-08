"""Tool policies: modes (off / shadow / enforce), full edits through the API, shape checks, and
default policies that are created once and stay deleted."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from boundary_agent.models import Policy
from boundary_agent.policy import PolicyEngine
from boundary_agent.types import ToolExecutionIntent


def intent(tool: str = "write_file") -> ToolExecutionIntent:
    return ToolExecutionIntent(
        conversation_id="c",
        run_id="r",
        server_id="s",
        server_name="local-sandbox",
        tool_name=tool,
        arguments={"path": "notes/a.txt"},
        token_budget=None,
        cost_budget=None,
        spent_tokens=0,
        spent_cost=0.0,
    )


def policy(mode: str, *, enabled: bool = True, pid: str = "p1") -> Policy:
    return Policy(
        id=pid,
        name="Approve writes",
        rule_type="require_approval",
        enabled=enabled,
        mode=mode,
        priority=100,
        target_tool="write_file",
        conditions_json=None,
        action_json={"reason": "Writes need a review"},
    )


# ---- engine ------------------------------------------------------------------------------------------


def test_shadow_policy_reports_what_it_would_do_and_allows():
    decision = PolicyEngine().evaluate(intent(), [policy("shadow")])
    assert decision.verdict == "allow"
    assert decision.shadow == [
        {"policy_id": "p1", "verdict": "require_approval", "reason": "Writes need a review"}
    ]


def test_enforced_policy_acts_and_shadow_ones_still_report():
    decision = PolicyEngine().evaluate(intent(), [policy("enforce"), policy("shadow", pid="p2")])
    assert decision.verdict == "require_approval"
    assert [s["policy_id"] for s in decision.shadow] == ["p2"]


@pytest.mark.parametrize(("mode", "enabled"), [("off", True), ("enforce", False), ("shadow", False)])
def test_off_or_disabled_policies_are_not_evaluated(mode, enabled):
    decision = PolicyEngine().evaluate(intent(), [policy(mode, enabled=enabled)])
    assert (decision.verdict, decision.shadow) == ("allow", [])


def test_policies_from_before_modes_are_enforced():
    legacy = policy("enforce")
    legacy.mode = None  # a row created before the column existed, before its default applies
    assert PolicyEngine().evaluate(intent(), [legacy]).verdict == "require_approval"


# ---- API ---------------------------------------------------------------------------------------------


@pytest.fixture
async def api():
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from boundary_agent import main
    from boundary_agent.db import Base, get_session

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def session():
        async with factory() as s:
            yield s

    main.app.dependency_overrides[get_session] = session
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, factory
    main.app.dependency_overrides.clear()
    await engine.dispose()


APPROVE_WRITES = {
    "name": "Approve writes",
    "rule_type": "require_approval",
    "target_tool": "write_file",
    "action": {"reason": "Writes need a review"},
}


async def test_policy_is_created_in_the_mode_asked_and_edited_in_full(api):
    client, _ = api
    created = await client.post("/api/policies", json={**APPROVE_WRITES, "mode": "shadow"})
    pid = created.json()["id"]
    [listed] = (await client.get("/api/policies")).json()
    assert (listed["mode"], listed["enabled"]) == ("shadow", True)

    # Every field can change, including what kind of policy it is.
    edit = {
        "name": "Notes only",
        "rule_type": "validate_args",
        "conditions": {"path_arg": "path", "allow_prefixes": ["notes/"]},
        "action": {},
        "mode": "enforce",
    }
    assert (await client.patch(f"/api/policies/{pid}", json=edit)).status_code == 200
    [listed] = (await client.get("/api/policies")).json()
    assert (listed["name"], listed["rule_type"], listed["mode"]) == ("Notes only", "validate_args", "enforce")
    assert listed["conditions"]["allow_prefixes"] == ["notes/"]


async def test_mode_and_enabled_stay_in_step(api):
    client, _ = api
    pid = (await client.post("/api/policies", json={**APPROVE_WRITES, "mode": "shadow"})).json()["id"]

    await client.patch(f"/api/policies/{pid}", json={"mode": "off"})
    [p] = (await client.get("/api/policies")).json()
    assert (p["mode"], p["enabled"]) == ("off", False)

    # The older on/off switch turns a policy back on in enforce.
    await client.patch(f"/api/policies/{pid}", json={"enabled": True})
    [p] = (await client.get("/api/policies")).json()
    assert (p["mode"], p["enabled"]) == ("enforce", True)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"rule_type": "token_budget", "conditions": {}}, "max_tokens"),
        ({"rule_type": "cost_budget", "conditions": {"max_cost": 0}}, "max_cost"),
        ({"rule_type": "validate_args", "conditions": {"path_arg": "path", "allow_prefixes": []}}, "folder"),
        ({"rule_type": "guard_signal", "action": {"verdict": "allow"}}, "verdict"),
    ],
)
async def test_policies_that_would_do_nothing_are_rejected(api, body, message):
    client, _ = api
    created = await client.post("/api/policies", json={"name": "x", **body})
    assert created.status_code == 422
    assert message in created.text

    # The same check applies to an edit, against the policy as it would end up.
    pid = (await client.post("/api/policies", json=APPROVE_WRITES)).json()["id"]
    edited = await client.patch(f"/api/policies/{pid}", json=body)
    assert edited.status_code == 422
    assert message in edited.text


async def test_default_policies_are_created_once_and_stay_deleted(api):
    from boundary_agent import main

    _, factory = api
    async with factory() as session:
        await main._seed_guard_signal_policies(session)
        await session.commit()
        defaults = (await session.scalars(select(Policy))).all()
        assert {p.target_tool for p in defaults} == {"write_file", "delete_file"}
        for p in defaults:
            await session.delete(p)
        await session.commit()

    # A restart doesn't bring them back.
    async with factory() as session:
        await main._seed_guard_signal_policies(session)
        await session.commit()
        assert (await session.scalars(select(Policy))).all() == []


async def test_databases_seeded_before_the_marker_are_not_seeded_again(api):
    from boundary_agent import main

    _, factory = api
    async with factory() as session:
        session.add(policy("enforce"))
        session.add(
            Policy(name="Old default", rule_type="guard_signal", enabled=True, target_tool="write_file")
        )
        await session.commit()
        await main._seed_guard_signal_policies(session)
        await session.commit()
        names = sorted(p.name for p in (await session.scalars(select(Policy))).all())
        assert names == ["Approve writes", "Old default"]


async def test_an_old_policy_that_fails_todays_checks_can_still_change_mode(api):
    _, factory = api
    from httpx import ASGITransport, AsyncClient

    from boundary_agent import main

    async with factory() as session:
        session.add(
            Policy(
                id="old",
                name="Old folder rule",
                rule_type="validate_args",
                enabled=True,
                mode="enforce",
                target_tool="write_file",
                conditions_json={"path_arg": "path", "allow_prefixes": []},
            )
        )
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.patch("/api/policies/old", json={"mode": "off"})).status_code == 200
        # Changing what it does is still checked.
        changed = await client.patch("/api/policies/old", json={"conditions": {"path_arg": "path"}})
        assert changed.status_code == 422


async def test_explicit_nulls_leave_fields_alone(api):
    client, _ = api
    pid = (await client.post("/api/policies", json=APPROVE_WRITES)).json()["id"]
    edited = await client.patch(f"/api/policies/{pid}", json={"mode": None, "name": None, "rule_type": None})
    assert edited.status_code == 200
    [p] = (await client.get("/api/policies")).json()
    assert (p["name"], p["rule_type"], p["mode"]) == ("Approve writes", "require_approval", "enforce")


async def test_budget_limits_must_be_numbers_not_booleans(api):
    client, _ = api
    body = {"name": "x", "rule_type": "token_budget", "conditions": {"max_tokens": True}}
    assert (await client.post("/api/policies", json=body)).status_code == 422


async def test_defaults_seeded_before_the_marker_are_labelled(api):
    from boundary_agent import main

    _, factory = api
    async with factory() as session:
        session.add(
            Policy(
                name="Tainted run: approve writes",
                rule_type="guard_signal",
                enabled=True,
                target_tool="write_file",
                action_json={"verdict": "require_approval"},
            )
        )
        await session.commit()
        await main._seed_guard_signal_policies(session)
        await session.commit()
        [p] = (await session.scalars(select(Policy))).all()
        assert p.action_json == {"verdict": "require_approval", "default": True}


def test_flagged_run_detail_goes_to_the_approval_not_the_user():
    # The taint detail names a guard policy; the user line keeps only the rule's own reason.
    tainted = intent()
    tainted.run_tainted = True
    tainted.taint_reason = "fetch_url output flagged by tool_output_injection_protectai (shadow)"
    rule = Policy(
        id="t",
        name="Tainted run: approve writes",
        rule_type="guard_signal",
        enabled=True,
        mode="enforce",
        priority=190,
        target_tool="write_file",
        conditions_json={"run_tainted": True},
        action_json={"verdict": "require_approval", "reason": "Tainted run: writes need approval."},
    )
    decision = PolicyEngine().evaluate(tainted, [rule])
    assert "tool_output_injection_protectai" in decision.reason
    assert decision.user_reason == "Tainted run: writes need approval."
