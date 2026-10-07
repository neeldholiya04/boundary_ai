"""The agent's adapter to `boundary_guard`.

The agent runtime calls `GuardAdapter.check()` at the four stages. The adapter runs the guard,
persists one `guard_decisions` row per policy, emits a `guard.decision` audit event, and works
out whether the run is now tainted. It never stores or publishes the checked text itself: rows
and events carry a redacted excerpt (every matched span replaced) and a sha256.

Decisions from async policies arrive later through `GuardDecisionSink`, which writes them with
its own database session.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from boundary_agent.audit import AuditLogger
from boundary_agent.models import Conversation, GuardDecision, Run
from boundary_agent.realtime import EventBroker
from boundary_agent.telemetry import DISABLED, Telemetry
from boundary_guard import (
    Action,
    CheckContext,
    DecisionEvent,
    Guard,
    GuardResult,
    Mode,
    PolicyDecision,
    Stage,
)
from boundary_guard.core.redact import redact
from boundary_guard.core.types import strongest

logger = logging.getLogger("boundary.guard")

EXCERPT_CHARS = 300
_EXCERPT_KEY = "_guard_excerpt"
_DIGEST_KEY = "_guard_sha256"


@dataclass(slots=True)
class GuardOutcome:
    result: GuardResult
    # Newly tainted by this check (injection flagged in tool output, enforced or shadow).
    taint_reason: str | None = None

    @property
    def action(self) -> Action:
        return self.result.action

    @property
    def text(self) -> str:
        return self.result.text

    def reason(self) -> str:
        """Human-readable summary of the policies that drove the applied action."""
        driving = [
            d
            for d in self.result.decisions
            if d.action is self.result.action and d.action is not Action.ALLOW
        ]
        if not driving:
            return "no policy fired"
        parts = []
        for d in driving:
            detail = f" ({d.reasons[0]})" if d.reasons else ""
            parts.append(f"{d.policy_id}{detail}")
        return "; ".join(parts)

    def policies(self, action: Action) -> list[str]:
        return [d.policy_id for d in self.result.decisions if d.action is action]


def _excerpt(text: str, decisions: list[PolicyDecision], sensitive: set[str]) -> str:
    """Redact spans found by sensitive policies (PII, secrets, anything that redacts). Spans from
    e.g. injection rules stay readable: the attack text is what a reviewer needs to see.

    `text` must be the text the spans index into (`GuardResult.checked_text`): after an enforced
    transform (secrets' redact_first) the other detectors' offsets are into the rewritten text, and a
    transform that rewrote has already removed its own matches from it."""
    spans = [span for d in decisions if d.policy_id in sensitive and not d.rewritten for span in d.spans]
    return redact(text, spans)[:EXCERPT_CHARS]


def _rows(
    excerpt: str,
    digest: str,
    stage: Stage,
    decisions: list[PolicyDecision],
    config_hash: str,
    *,
    run_id: str | None,
    conversation_id: str | None,
    tool_name: str | None,
) -> list[GuardDecision]:
    return [
        GuardDecision(
            run_id=run_id,
            conversation_id=conversation_id,
            stage=stage.value,
            tool_name=tool_name,
            policy_id=d.policy_id,
            policy_version=d.policy_version,
            config_hash=config_hash,
            detector=d.detector,
            mode=d.mode.value,
            execution=d.execution.value,
            action=d.action.value,
            would_action=d.would_action.value,
            score=d.score,
            threshold=d.threshold,
            latency_ms=d.latency_ms,
            cost_usd=d.cost_usd,
            reasons_json=list(d.reasons),
            span_labels_json=sorted({span.label for span in d.spans}),
            error=d.error,
            excerpt=excerpt,
            content_sha256=digest,
        )
        for d in decisions
    ]


def decision_payload(
    stage: Stage, decisions: list[PolicyDecision], config_hash: str, *, tool_name: str | None, action: Action
) -> dict[str, Any]:
    """Audit-event payload. Matched text is deliberately left out: spans can cover secrets and PII."""
    return {
        "stage": stage.value,
        "tool_name": tool_name,
        "action": action.value,
        "would_action": strongest([d.would_action for d in decisions]).value,
        "config_hash": config_hash,
        "decisions": [
            {
                "policy_id": d.policy_id,
                "mode": d.mode.value,
                "execution": d.execution.value,
                "action": d.action.value,
                "would_action": d.would_action.value,
                "score": d.score,
                "reasons": d.reasons,
                "span_labels": sorted({span.label for span in d.spans}),
                "latency_ms": round(d.latency_ms, 2),
                "error": d.error,
            }
            for d in decisions
        ],
    }


class GuardAdapter:
    def __init__(
        self,
        guard: Guard,
        audit_logger: AuditLogger,
        *,
        taint_labels: list[str] | None = None,
        telemetry: Telemetry = DISABLED,
    ) -> None:
        self.guard = guard
        self.audit_logger = audit_logger
        self.telemetry = telemetry
        self.taint_labels = set(taint_labels or ["injection"])
        self._sets_for: str | None = None
        self._sensitive: set[str] = set()
        self._taint: set[str] = set()

    def _refresh(self) -> None:
        # Rules added in the dashboard change the policy set at runtime: recompute when the config does.
        if self._sets_for == self.guard.config_hash:
            return
        policies = self.guard.config.policies
        # Operator rules that match words or patterns are usually about sensitive terms (codenames,
        # account numbers), so their matches are kept out of stored excerpts whatever the rule does.
        self._sensitive = {
            p.id
            for p in policies
            if p.action is Action.REDACT
            or {"pii", "secret"}.intersection(p.detects)
            or p.detector.type in {"keywords", "pattern"}
        }
        self._taint = {
            p.id
            for p in policies
            if Stage.TOOL_OUTPUT in p.stages and self.taint_labels.intersection(p.detects)
        }
        self._sets_for = self.guard.config_hash

    @property
    def sensitive_policies(self) -> set[str]:
        """Policies whose matches are sensitive and get redacted in stored excerpts."""
        self._refresh()
        return self._sensitive

    @property
    def taint_policies(self) -> set[str]:
        """Tool-output policies whose firing (enforced or shadow) taints the run."""
        self._refresh()
        return self._taint

    async def check(
        self,
        session: AsyncSession,
        stage: Stage,
        text: str,
        *,
        conversation: Conversation,
        run: Run,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        references: list[str] | None = None,
        response_schema: str | None = None,
    ) -> GuardOutcome:
        ctx = CheckContext(
            run_id=run.id,
            conversation_id=conversation.id,
            tool_name=tool_name,
            tool_args=tool_args,
            references=references or [],
            response_schema=response_schema,
        )
        with self.telemetry.guard_check(stage.value) as span:
            result = await self.guard.check(stage, text, ctx)
            # Redacted with the sensitive spans the blocking policies found. Set before the next await,
            # so async policies scheduled by this check see it when their decisions reach the sink;
            # the raw text itself is never handed on (and the trace only gets this excerpt).
            checked = result.checked_text if result.checked_text is not None else text
            excerpt = _excerpt(checked, result.decisions, self.sensitive_policies)
            span.record(result, excerpt)
        digest = hashlib.sha256(text.encode()).hexdigest()
        ctx.metadata[_EXCERPT_KEY] = excerpt
        ctx.metadata[_DIGEST_KEY] = digest
        session.add_all(
            _rows(
                excerpt,
                digest,
                stage,
                result.decisions,
                result.config_hash,
                run_id=run.id,
                conversation_id=conversation.id,
                tool_name=tool_name,
            )
        )
        await self.audit_logger.record(
            session,
            "guard.decision",
            {
                **decision_payload(
                    stage, result.decisions, result.config_hash, tool_name=tool_name, action=result.action
                ),
                "latency_ms": round(result.latency_ms, 2),
                "pending_async": result.pending_async,
            },
            conversation_id=conversation.id,
            run_id=run.id,
        )

        taint_reason = None
        if stage is Stage.TOOL_OUTPUT:
            flagged = [
                d
                for d in result.decisions
                if d.policy_id in self.taint_policies and d.would_action is not Action.ALLOW
            ]
            if flagged:
                modes = ", ".join(f"{d.policy_id} ({d.mode.value})" for d in flagged)
                taint_reason = f"{tool_name or 'tool'} output flagged by {modes}"
        return GuardOutcome(result, taint_reason)


class GuardDecisionSink:
    """Persists decisions from async policies (they complete after the request has returned)."""

    def __init__(self, session_factory: Callable[[], AsyncSession], broker: EventBroker) -> None:
        self.session_factory = session_factory
        self.broker = broker

    async def record(self, event: DecisionEvent) -> None:
        if event.ctx.metadata.get("source", "app") != "app":
            return  # playground scans and startup warm-up are not agent runs: nothing is stored
        if not event.is_async:
            return  # blocking decisions are persisted by GuardAdapter.check, in the request's session
        excerpt = str(event.ctx.metadata.get(_EXCERPT_KEY, ""))
        digest = str(event.ctx.metadata.get(_DIGEST_KEY, ""))
        async with self.session_factory() as session:
            session.add_all(
                _rows(
                    excerpt,
                    digest,
                    event.stage,
                    event.decisions,
                    event.config_hash,
                    run_id=event.ctx.run_id,
                    conversation_id=event.ctx.conversation_id,
                    tool_name=event.ctx.tool_name,
                )
            )
            await session.commit()
        payload = decision_payload(
            event.stage,
            event.decisions,
            event.config_hash,
            tool_name=event.ctx.tool_name,
            action=Action.ALLOW,
        )
        await self.broker.publish({"type": "guard.async_decision", "payload": payload})


PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9_]*_\d+>")


def redaction_notice(outcome: GuardOutcome | None, original: str) -> str | None:
    """Tell the user what the guard removed from their own message, by placeholder and policy.

    Without this a pasted key silently becomes `<OPENAI_KEY_1>`, and a file the user asked for gets
    the placeholder instead of the value with nothing to say why.
    """
    if outcome is None or outcome.text == original:
        return None
    placeholders = sorted(set(PLACEHOLDER.findall(outcome.text)) - set(PLACEHOLDER.findall(original)))
    if not placeholders:
        return None
    policies = ", ".join(outcome.policies(Action.REDACT)) or "the guard"
    return (
        f"Removed from your message before it was stored or sent to the model: {', '.join(placeholders)} "
        f"({policies}). The assistant only sees the placeholder, so it can't use or write the original value."
    )


def withheld_result(outcome: GuardOutcome, *, reviewed: bool = False) -> dict[str, Any]:
    """What the planner (and the Message table) sees in place of blocked tool output."""
    policies = outcome.policies(Action.BLOCK) or outcome.policies(Action.ESCALATE)
    return {
        "withheld_by_guard": {
            "policies": policies,
            "reason": "denied in content review" if reviewed else outcome.reason(),
            "note": "The tool returned content the guard would not pass to the model. Continue without it.",
        }
    }


def redacted_tool_result(original: Any, redacted_text: str) -> Any:
    """Rebuild a tool result from the guard's (redacted) text. `raw` is dropped: it repeats the
    unredacted content."""
    if isinstance(original, dict) and isinstance(original.get("content"), str):
        is_error = bool((original.get("raw") or {}).get("isError"))
        return {"content": redacted_text, "redacted_by_guard": True, "is_error": is_error}
    try:
        return json.loads(redacted_text)
    except json.JSONDecodeError:
        return {"content": redacted_text, "redacted_by_guard": True}


def apply_overrides(guard: Guard, overrides: list[tuple[str, str]]) -> list[str]:
    """Apply persisted (policy_id, mode) overrides to the in-memory guard. Ignores unknown policies
    (a policy may have been removed since the override was saved). Returns the ids applied."""
    known = set(guard.policy_ids)
    applied = []
    for policy_id, mode in overrides:
        if policy_id in known:
            guard.set_mode(policy_id, Mode(mode))
            applied.append(policy_id)
    return applied


def aggregate_stats(guard: Guard, decisions: list[Any]) -> list[dict[str, Any]]:
    """Per-policy would-action breakdown from recent decision rows (the shadow-vs-enforce view).
    `decisions` are objects with policy_id / would_action / error / latency_ms attributes."""
    per: dict[str, dict[str, Any]] = {}
    for p in guard.config.policies:
        per[p.id] = {
            "policy_id": p.id,
            "mode": guard.mode_of(p.id).value,
            "execution": p.execution.value,
            "action": p.action.value,
            "checks": 0,
            "would_fire": 0,
            "would_actions": {},
            "errors": 0,
            "_latencies": [],
        }
    for row in decisions:
        stat = per.get(row.policy_id)
        if stat is None:
            continue
        stat["checks"] += 1
        if row.would_action != "allow":
            stat["would_fire"] += 1
            stat["would_actions"][row.would_action] = stat["would_actions"].get(row.would_action, 0) + 1
        if row.error:
            stat["errors"] += 1
        if row.latency_ms is not None:
            stat["_latencies"].append(row.latency_ms)
    out = []
    for stat in per.values():
        lat = sorted(stat.pop("_latencies"))
        n = stat["checks"]
        stat["would_fire_rate"] = (stat["would_fire"] / n) if n else None
        stat["p50_ms"] = lat[len(lat) // 2] if lat else None
        stat["p99_ms"] = lat[min(len(lat) - 1, int(len(lat) * 0.99))] if lat else None
        out.append(stat)
    return out


def mode_counts(guard: Guard) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pid in guard.policy_ids:
        mode = guard.mode_of(pid)
        counts[mode.value] = counts.get(mode.value, 0) + 1
    return counts


__all__ = [
    "GuardAdapter",
    "GuardDecisionSink",
    "GuardOutcome",
    "Mode",
    "aggregate_stats",
    "apply_overrides",
    "decision_payload",
    "mode_counts",
    "redacted_tool_result",
    "redaction_notice",
    "withheld_result",
]
