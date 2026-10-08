"""Guard rules written in the dashboard (spec, compilation and dry runs: rules.py). Each change updates the
live guard and the database together, one at a time, and is audited with the full spec."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boundary_agent import services
from boundary_agent.config import REPO_ROOT
from boundary_agent.db import get_session
from boundary_agent.models import GuardDecision, GuardRule
from boundary_agent.rules import RULE_PREFIX, RuleSpec, benign_records, compile_rule, dry_run, policy_id_for
from boundary_agent.schemas import GuardModeRequest, GuardRuleTestRequest
from boundary_guard import Guard, PolicyConfig

router = APIRouter(prefix="/api/guard")


# Rules that failed to load at startup (e.g. a judge model that's gone), by policy id. They stay in the
# database and the dashboard so they can be fixed; the guard runs without them meanwhile.
rule_errors: dict[str, str] = {}
# One rule change at a time: each one updates the live guard and the database, and they must agree.
rule_lock = asyncio.Lock()


def judge_model() -> str:
    return services.settings.guard_judge_model or services.settings.llm_model


def judge_models() -> set[str]:
    """Models a judge rule may name: the configured ones, so a rule can't send text to an arbitrary
    provider (or run up a bill on an expensive model)."""
    return {judge_model(), services.settings.llm_model, *services.settings.guard_judge_models}


def _require_guard() -> Guard:
    if services.guard is None:
        raise HTTPException(
            status_code=409, detail="The guard is off (no GUARD_POLICY_PATH), so rules can't run"
        )
    return services.guard


def _rule_view(rule: GuardRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "policy_id": rule.policy_id,
        "version": rule.version,
        "spec": rule.spec_json,
        # In the guard: running (in any mode but off). A rule switched off, or one that failed to
        # load, is stored but not in the guard.
        "active": services.guard is not None and rule.policy_id in services.guard.policy_ids,
        "error": rule_errors.get(rule.policy_id),
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }


def _parse_spec(raw: dict[str, Any]) -> RuleSpec:
    try:
        spec = RuleSpec.model_validate(raw)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if spec.check.type == "llm_judge" and spec.check.model and spec.check.model not in judge_models():
        allowed = ", ".join(sorted(judge_models()))
        raise HTTPException(status_code=422, detail=f"judge model must be one of: {allowed}")
    return spec


def _compile(spec: RuleSpec, policy_id: str) -> PolicyConfig:
    try:
        return compile_rule(
            spec, policy_id, judge_model=judge_model(), taint_labels=services.settings.guard_taint_labels
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _apply(live: Guard, policy_id: str, spec: RuleSpec) -> None:
    """Put `spec` into the live guard. A rule switched off is taken out instead, so a rule whose
    check can't currently be built (a removed judge model) can still be switched off."""
    if spec.mode == "off":
        if policy_id in live.policy_ids:
            live.remove_policy(policy_id)
        return
    try:
        live.add_policy(_compile(spec, policy_id), replace=policy_id in live.policy_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _restore(live: Guard, policy_id: str, previous: PolicyConfig | None) -> None:
    """Undo `_apply` after a failed commit, so the guard keeps matching the database."""
    with suppress(ValueError, KeyError):
        if previous is None:
            live.remove_policy(policy_id)
        else:
            live.add_policy(previous, replace=policy_id in live.policy_ids)


async def _save_rule(session: AsyncSession, rule: GuardRule, spec: RuleSpec, *, event: str) -> dict[str, Any]:
    """Apply `spec` to the live guard first (an invalid rule changes nothing), then persist and audit;
    if the commit fails, the guard goes back to what it was."""
    live = _require_guard()
    previous = next((p for p in live.config.policies if p.id == rule.policy_id), None)
    _apply(live, rule.policy_id, spec)
    rule_errors.pop(rule.policy_id, None)
    rule.spec_json = spec.model_dump(mode="json")
    if event != "guard.rule_created":
        rule.version += 1
    await services.audit_logger.record(
        session,
        event,
        {
            "rule_id": rule.id,
            "policy_id": rule.policy_id,
            "version": rule.version,
            "spec": rule.spec_json,
            "config_hash": live.config_hash,
        },
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        _restore(live, rule.policy_id, previous)
        raise
    services.telemetry.metadata["config_hash"] = live.config_hash
    return _rule_view(rule)


async def set_rule_mode(session: AsyncSession, rule: GuardRule, mode: str) -> dict[str, Any]:
    async with rule_lock:
        if rule.spec_json.get("mode") == mode and (
            mode == "off" or rule.policy_id in _require_guard().policy_ids
        ):
            return {"policy_id": rule.policy_id, "mode": mode, "config_hash": _require_guard().config_hash}
        spec = _parse_spec({**rule.spec_json, "mode": mode})
        await _save_rule(session, rule, spec, event="guard.rule_updated")
    return {"policy_id": rule.policy_id, "mode": mode, "config_hash": _require_guard().config_hash}


async def load_guard_rules(session: AsyncSession) -> None:
    rule_errors.clear()
    for rule in (await session.scalars(select(GuardRule).order_by(GuardRule.created_at))).all():
        try:
            spec = RuleSpec.model_validate(rule.spec_json)
            if spec.mode != "off":
                services.guard.add_policy(
                    compile_rule(
                        spec,
                        rule.policy_id,
                        judge_model=judge_model(),
                        taint_labels=services.settings.guard_taint_labels,
                    ),
                    replace=True,
                )
        except Exception as exc:  # one broken rule must not stop the app (or the other rules) loading
            rule_errors[rule.policy_id] = f"{type(exc).__name__}: {exc}"
            await services.audit_logger.record(
                session,
                "guard.rule_failed",
                {"rule_id": rule.id, "policy_id": rule.policy_id, "error": str(exc)},
            )
    services.telemetry.metadata["config_hash"] = services.guard.config_hash


async def _used_policy_ids(session: AsyncSession, live: Guard) -> set[str]:
    """Ids a new rule may not take: live policies, stored rules, and any id with decision history, so a
    recreated rule never inherits a deleted one's shadow/enforce statistics."""
    stored = set((await session.scalars(select(GuardRule.policy_id))).all())
    seen = set(
        (
            await session.scalars(
                select(GuardDecision.policy_id)
                .where(GuardDecision.policy_id.like(f"{RULE_PREFIX}%"))
                .distinct()
            )
        ).all()
    )
    return set(live.policy_ids) | stored | seen


@router.get("/rules")
async def list_guard_rules(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rules = (await session.scalars(select(GuardRule).order_by(GuardRule.created_at))).all()
    return [_rule_view(rule) for rule in rules]


@router.post("/rules", status_code=201)
async def create_guard_rule(payload: dict[str, Any], session: AsyncSession = Depends(get_session)) -> dict:
    live = _require_guard()
    spec = _parse_spec(payload)
    async with rule_lock:
        rule = GuardRule(
            policy_id=policy_id_for(spec.name, await _used_policy_ids(session, live)), spec_json={}, version=1
        )
        session.add(rule)
        await session.flush()
        try:
            return await _save_rule(session, rule, spec, event="guard.rule_created")
        except HTTPException:
            await session.rollback()
            raise


@router.put("/rules/{rule_id}")
async def replace_guard_rule(
    rule_id: str, payload: dict[str, Any], session: AsyncSession = Depends(get_session)
) -> dict:
    spec = _parse_spec(payload)
    async with rule_lock:
        rule = await session.get(GuardRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        return await _save_rule(session, rule, spec, event="guard.rule_updated")


@router.patch("/rules/{rule_id}")
async def set_guard_rule_mode(
    rule_id: str, payload: GuardModeRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    rule = await session.get(GuardRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    await set_rule_mode(session, rule, payload.mode)
    return _rule_view(rule)


@router.delete("/rules/{rule_id}")
async def delete_guard_rule(rule_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    live = _require_guard()
    async with rule_lock:
        rule = await session.get(GuardRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        previous = next((p for p in live.config.policies if p.id == rule.policy_id), None)
        if previous is not None:
            live.remove_policy(rule.policy_id)
        await services.audit_logger.record(
            session,
            "guard.rule_deleted",
            {
                "rule_id": rule.id,
                "policy_id": rule.policy_id,
                "version": rule.version,
                "spec": rule.spec_json,
                "config_hash": live.config_hash,
            },
        )
        await session.delete(rule)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            _restore(live, rule.policy_id, previous)
            raise
        rule_errors.pop(rule.policy_id, None)
        services.telemetry.metadata["config_hash"] = live.config_hash
    return {"ok": True, "config_hash": live.config_hash}


@router.post("/rules/test")
async def test_guard_rule(payload: GuardRuleTestRequest, request: Request) -> dict:
    """Dry-run a draft rule against its examples and benign eval records. Nothing is stored or applied."""
    spec = _parse_spec(payload.spec)
    _compile(spec, "rule_draft")
    judged = spec.check.type == "llm_judge"
    if judged:
        # Every example and sampled record is a model call: bound both, and how often it can be asked.
        cap = services.settings.guard_rule_test_examples_judge
        if len(spec.tests.should_fire) + len(spec.tests.should_pass) > cap:
            raise HTTPException(status_code=422, detail=f"judge rules are tested on at most {cap} examples")
        allowed, retry_after = await services.limiter.hit(
            "rule_test_judge", request.client.host if request.client else "?", limit=20, window_s=3600
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many judged dry runs; try again later.",
                headers={"Retry-After": str(retry_after)},
            )
    sample = (
        services.settings.guard_rule_test_sample_judge if judged else services.settings.guard_rule_test_sample
    )
    benign = benign_records(REPO_ROOT, spec.stages, limit=sample) if payload.sample_benign else None
    try:
        return await dry_run(
            spec,
            base_dir=services.guard_policy_path.parent
            if services.guard_policy_path
            else REPO_ROOT / "policies",
            judge_model=judge_model(),
            taint_labels=services.settings.guard_taint_labels,
            benign_texts=benign,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/rules/export", response_class=Response)
async def export_guard_rules(session: AsyncSession = Depends(get_session)) -> Response:
    """The running rules as a policy file fragment (same format as policies/guard.yaml), so a rule that
    has proved itself can be reviewed into the baseline, or measured with `boundary-eval detectors`."""
    import yaml

    live = _require_guard()
    rules = (await session.scalars(select(GuardRule).order_by(GuardRule.created_at))).all()
    running = {r.policy_id for r in rules} & set(live.policy_ids)
    policies = [p.model_dump(mode="json", exclude_none=True) for p in live.config.policies if p.id in running]
    left_out = sorted({r.policy_id for r in rules} - running)
    header = (
        f"# Guard rules exported from the dashboard. Config {live.config_hash}, "
        f"{datetime.now(UTC).isoformat(timespec='seconds')}.\n"
        "# Paths are relative to policies/; measure with:\n"
        "#   uv run boundary-eval detectors --suite golden --policies <this file>\n"
        + (f"# Not included (switched off or failed to load): {', '.join(left_out)}\n" if left_out else "")
    )
    body = yaml.safe_dump({"version": live.config.version, "policies": policies}, sort_keys=False)
    return Response(header + body, media_type="application/yaml")
