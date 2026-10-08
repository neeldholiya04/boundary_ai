"""The guard as a whole: status and policies, per-policy modes (shadow/enforce/off), recent decisions and
per-policy stats, and the check types the rule editor offers. Rules themselves: api/guard_rules.py."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boundary_agent import services
from boundary_agent.api import guard_rules
from boundary_agent.db import get_session
from boundary_agent.guarding import aggregate_stats, apply_overrides, mode_counts
from boundary_agent.models import GuardDecision, GuardOverride, GuardRule
from boundary_agent.rules import RULE_PREFIX
from boundary_agent.schemas import GuardModeRequest
from boundary_guard import Mode, Stage

router = APIRouter(prefix="/api/guard")


@router.get("/status")
async def guard_status() -> dict:
    if services.guard is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "policy_file": str(services.guard_policy_path),
        "version": services.guard.config.version,
        "config_hash": services.guard.config_hash,
        "modes": mode_counts(services.guard),
        "policies": [
            {
                "id": p.id,
                # file: the reviewed baseline in the policy file; rule: written in the dashboard.
                "origin": "file" if services.guard.is_file_policy(p.id) else "rule",
                "description": p.description,
                "tools": p.tools,
                "stages": [stage.value for stage in p.stages],
                "detector": p.detector.type,
                "action": p.action.value,
                "mode": services.guard.mode_of(p.id).value,
                "execution": p.execution.value,
                "detects": p.detects,
                "threshold": services.guard.threshold_of(p.id),
            }
            for p in services.guard.config.policies
        ],
        "guard_rules.rule_errors": guard_rules.rule_errors,
        "dropped_async": services.guard.dropped_async,
        "pending_async": services.guard.pending_async,
        "tracing": services.telemetry.enabled,
    }


async def apply_guard_overrides(session: AsyncSession) -> None:
    if services.guard is None:
        return
    rows = (await session.scalars(select(GuardOverride))).all()
    apply_overrides(services.guard, [(o.policy_id, o.mode) for o in rows])
    services.telemetry.metadata["config_hash"] = services.guard.config_hash


@router.patch("/policies/{policy_id}")
async def set_guard_mode(
    policy_id: str, payload: GuardModeRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    if services.guard is None:
        raise HTTPException(status_code=404, detail="Guard is not enabled")
    if not services.guard.is_file_policy(policy_id) and not policy_id.startswith(RULE_PREFIX):
        raise HTTPException(status_code=404, detail=f"Unknown policy {policy_id}")

    if not services.guard.is_file_policy(policy_id):
        # A rule's mode is part of the rule (versioned and audited with it), not an override.
        rule = await session.scalar(select(GuardRule).where(GuardRule.policy_id == policy_id))
        if rule is None:
            raise HTTPException(status_code=404, detail=f"Unknown policy {policy_id}")
        return await guard_rules.set_rule_mode(session, rule, payload.mode)

    previous = services.guard.mode_of(policy_id).value
    services.guard.set_mode(policy_id, Mode(payload.mode))
    services.telemetry.metadata["config_hash"] = services.guard.config_hash  # traces carry the hash in force
    # Persist as an override, or delete it when the mode matches the YAML default.
    default = next(p for p in services.guard.config.policies if p.id == policy_id).mode.value
    existing = await session.get(GuardOverride, policy_id)
    if payload.mode == default:
        if existing is not None:
            await session.delete(existing)
    elif existing is not None:
        existing.mode = payload.mode
    else:
        session.add(GuardOverride(policy_id=policy_id, mode=payload.mode))
    await services.audit_logger.record(
        session,
        "guard.mode_changed",
        {
            "policy_id": policy_id,
            "from": previous,
            "to": payload.mode,
            "config_hash": services.guard.config_hash,
        },
    )
    await session.commit()
    return {"policy_id": policy_id, "mode": payload.mode, "config_hash": services.guard.config_hash}


@router.get("/check-types")
async def guard_check_types() -> dict:
    """What a rule can check, for the dashboard's rule form."""
    return {
        "stages": [s.value for s in Stage],
        "tool_stages": [Stage.TOOL_ARGS.value, Stage.TOOL_OUTPUT.value],
        "judge_model": guard_rules.judge_model(),
        "checks": [
            {
                "type": "keywords",
                "label": "Keywords",
                "can_redact": True,
                "hint": "Words or phrases, matched as whole words, any case.",
            },
            {
                "type": "pattern",
                "label": "Pattern",
                "can_redact": True,
                "hint": "Regular expressions; all of a rule's patterns share a 0.25 s budget per check.",
            },
            {
                "type": "topic",
                "label": "Topic",
                "can_redact": False,
                "hint": "Example requests about the topic; compared by meaning, not words.",
            },
            {
                "type": "llm_judge",
                "label": "Plain-language policy",
                "can_redact": False,
                "hint": (
                    "An LLM decides. Sends the checked text to the judge model's provider, costs a call "
                    "per check, and blocks if the judge errors: start in shadow."
                ),
            },
            {
                "type": "always",
                "label": "Every call",
                "can_redact": False,
                "hint": "For tool rules: every call to the chosen tools gets the action.",
            },
        ],
    }


@router.get("/stats")
async def guard_stats(session: AsyncSession = Depends(get_session), limit: int = 5000) -> dict:
    """Per-policy would-action breakdown from recent decisions, for the shadow-vs-enforce view."""
    if services.guard is None:
        return {"enabled": False}
    rows = (
        await session.scalars(select(GuardDecision).order_by(GuardDecision.created_at.desc()).limit(limit))
    ).all()
    return {
        "enabled": True,
        "sampled": len(rows),
        "config_hash": services.guard.config_hash,
        "policies": aggregate_stats(services.guard, rows),
    }


@router.get("/decisions")
async def list_guard_decisions(
    session: AsyncSession = Depends(get_session),
    run_id: str | None = None,
    policy_id: str | None = None,
    fired_only: bool = False,
    limit: int = 200,
) -> list[dict]:
    query = select(GuardDecision).order_by(GuardDecision.created_at.desc()).limit(min(limit, 1000))
    if run_id:
        query = query.where(GuardDecision.run_id == run_id)
    if policy_id:
        query = query.where(GuardDecision.policy_id == policy_id)
    if fired_only:
        query = query.where(GuardDecision.would_action != "allow")
    rows = (await session.scalars(query)).all()
    return [
        {
            "id": row.id,
            "run_id": row.run_id,
            "conversation_id": row.conversation_id,
            "stage": row.stage,
            "tool_name": row.tool_name,
            "policy_id": row.policy_id,
            "mode": row.mode,
            "execution": row.execution,
            "action": row.action,
            "would_action": row.would_action,
            "score": row.score,
            "threshold": row.threshold,
            "latency_ms": row.latency_ms,
            "reasons": row.reasons_json,
            "span_labels": row.span_labels_json,
            "error": row.error,
            "excerpt": row.excerpt,
            "config_hash": row.config_hash,
            "created_at": row.created_at,
        }
        for row in rows
    ]
