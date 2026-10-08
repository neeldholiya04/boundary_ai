"""Tool rules (the policy engine): decide a tool call by the tool and its arguments. See policy.py."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boundary_agent import services
from boundary_agent.db import get_session
from boundary_agent.models import Policy
from boundary_agent.policy import policy_mode
from boundary_agent.schemas import PolicyCreate, PolicyUpdate, check_policy_shape

router = APIRouter(prefix="/api/policies")


@router.get("")
async def list_policies(session: AsyncSession = Depends(get_session)) -> list[dict]:
    policies = (
        await session.scalars(select(Policy).order_by(Policy.priority.desc(), Policy.created_at.desc()))
    ).all()
    return [
        {
            "id": policy.id,
            "name": policy.name,
            "rule_type": policy.rule_type,
            "enabled": policy.enabled,
            "mode": policy_mode(policy),
            "priority": policy.priority,
            "target_tool": policy.target_tool,
            "target_server_id": policy.target_server_id,
            "conditions": policy.conditions_json,
            "action": policy.action_json,
            "created_at": policy.created_at,
            "updated_at": policy.updated_at,
        }
        for policy in policies
    ]


def _policy_audit(policy: Policy) -> dict:
    """Everything that decides what a policy does, so the log shows each version."""
    return {
        "policy_id": policy.id,
        "name": policy.name,
        "rule_type": policy.rule_type,
        "mode": policy_mode(policy),
        "priority": policy.priority,
        "target_tool": policy.target_tool,
        "conditions": policy.conditions_json,
        "action": policy.action_json,
    }


@router.post("")
async def create_policy(payload: PolicyCreate, session: AsyncSession = Depends(get_session)) -> dict:
    mode = payload.mode if payload.enabled else "off"
    policy = Policy(
        name=payload.name,
        rule_type=payload.rule_type,
        enabled=mode != "off",
        mode=mode,
        priority=payload.priority,
        target_tool=payload.target_tool,
        target_server_id=payload.target_server_id,
        conditions_json=payload.conditions,
        action_json=payload.action,
    )
    session.add(policy)
    await session.flush()
    await services.audit_logger.record(session, "policy.created", _policy_audit(policy))
    await session.commit()
    return {"id": policy.id}


@router.patch("/{policy_id}")
async def update_policy(
    policy_id: str, payload: PolicyUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    policy = await session.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    changes = payload.model_dump(exclude_unset=True)
    # An explicit null only means something for the scope fields (a budget has no tool).
    changes = {k: v for k, v in changes.items() if v is not None or k in ("target_tool", "target_server_id")}
    # `mode` and the older `enabled` describe the same switch; keep them in step.
    if "mode" in changes:
        changes["enabled"] = changes["mode"] != "off"
    elif "enabled" in changes:
        current = policy_mode(policy)
        changes["mode"] = ("enforce" if current == "off" else current) if changes["enabled"] else "off"
    # Only a change to what the policy does is re-validated, so an old policy that fails today's checks
    # can still be switched off or to shadow.
    try:
        if {"rule_type", "conditions", "action"} & changes.keys():
            check_policy_shape(
                changes.get("rule_type", policy.rule_type),
                changes.get("conditions", policy.conditions_json),
                changes.get("action", policy.action_json),
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for field_name, value in changes.items():
        if field_name == "conditions":
            policy.conditions_json = value
        elif field_name == "action":
            policy.action_json = value
        else:
            setattr(policy, field_name, value)
    await services.audit_logger.record(session, "policy.updated", _policy_audit(policy))
    await session.commit()
    return {"ok": True}


@router.delete("/{policy_id}")
async def delete_policy(policy_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    policy = await session.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    await services.audit_logger.record(
        session, "policy.deleted", {"policy_id": policy.id, "name": policy.name}
    )
    await session.delete(policy)
    await session.commit()
    return {"ok": True}
