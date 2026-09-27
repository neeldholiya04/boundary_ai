from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from boundary_guard.core.config import GuardConfig, PolicyConfig, load_config
from boundary_guard.core.detector import Detector, build_detector
from boundary_guard.core.redact import redact
from boundary_guard.core.types import (
    Action,
    CheckContext,
    Detection,
    Execution,
    GuardResult,
    Mode,
    OnError,
    PolicyDecision,
    Stage,
    strongest,
)

logger = logging.getLogger("boundary_guard")


@dataclass(slots=True)
class DecisionEvent:
    stage: Stage
    ctx: CheckContext
    decisions: list[PolicyDecision]
    config_hash: str
    is_async: bool


class DecisionSink(Protocol):
    async def record(self, event: DecisionEvent) -> None: ...


@dataclass(slots=True)
class _BoundPolicy:
    config: PolicyConfig
    detector: Detector


class Guard:
    """Runs the configured policies for one stage of the agent loop.

    Order within a check:
      1. transform policies, sequentially (each sees the previous one's rewrite)
      2. detector policies, concurrently, on the transformed text
      3. redactions from enforced REDACT decisions are applied to the text
      4. async policies are scheduled in the background; their decisions go to sinks only
    """

    def __init__(
        self,
        config: GuardConfig,
        *,
        base_dir: str | Path = ".",
        sinks: list[DecisionSink] | None = None,
        max_pending_async: int = 256,
    ) -> None:
        import boundary_guard.detectors  # noqa: F401  (registers built-in detector types)

        self.config = config
        self.base_dir = Path(base_dir)
        self.sinks: list[DecisionSink] = list(sinks or [])
        self.max_pending_async = max_pending_async
        self.dropped_async = 0

        self._policies: dict[str, _BoundPolicy] = {}
        self._by_stage: dict[Stage, list[_BoundPolicy]] = {stage: [] for stage in Stage}
        for policy in config.policies:
            detector = build_detector(policy.detector.type, policy.detector.params(), self.base_dir)
            bound = _BoundPolicy(policy, detector)
            self._policies[policy.id] = bound
            for stage in policy.stages:
                self._by_stage[stage].append(bound)

        self._overrides: dict[str, Mode] = {}
        self._pending: set[asyncio.Task[None]] = set()
        self._config_hash = self._compute_hash()

    @classmethod
    def from_yaml(cls, path: str | Path, **kwargs) -> Guard:
        path = Path(path)
        return cls(load_config(path), base_dir=path.parent, **kwargs)

    # ---- configuration -------------------------------------------------------------

    @property
    def config_hash(self) -> str:
        return self._config_hash

    @property
    def policy_ids(self) -> list[str]:
        return list(self._policies)

    def mode_of(self, policy_id: str) -> Mode:
        bound = self._policies[policy_id]
        return self._overrides.get(policy_id, bound.config.mode)

    def threshold_of(self, policy_id: str) -> float | None:
        return self._policies[policy_id].detector.threshold

    def set_mode(self, policy_id: str, mode: Mode | None) -> None:
        """Runtime override of a policy's mode (None clears it). Changes the config hash."""
        if policy_id not in self._policies:
            raise KeyError(policy_id)
        if mode is None or mode == self._policies[policy_id].config.mode:
            self._overrides.pop(policy_id, None)
        else:
            self._overrides[policy_id] = mode
        self._config_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        payload = {
            "config": self.config.model_dump(mode="json"),
            "fingerprints": {pid: b.detector.fingerprint() for pid, b in sorted(self._policies.items())},
            "overrides": {pid: mode.value for pid, mode in sorted(self._overrides.items())},
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    # ---- checking ------------------------------------------------------------------

    async def check(self, stage: Stage, text: str, ctx: CheckContext | None = None) -> GuardResult:
        ctx = ctx or CheckContext()
        started = time.perf_counter()

        active = [
            b
            for b in self._by_stage[stage]
            if self.mode_of(b.config.id) is not Mode.OFF and b.detector.applies(ctx)
        ]
        blocking = [b for b in active if b.config.execution is Execution.BLOCKING]
        transforms = [b for b in blocking if b.detector.transform]
        detectors = [b for b in blocking if not b.detector.transform]
        deferred = [b for b in active if b.config.execution is Execution.ASYNC]

        decisions: list[PolicyDecision] = []
        current = text
        for bound in transforms:
            decision, detection = await self._run(bound, stage, current, ctx)
            if (
                detection is not None
                and detection.rewrite is not None
                and decision.mode is Mode.ENFORCE
                and decision.action is not Action.BLOCK
            ):
                current = detection.rewrite
                decision.rewritten = True
            decisions.append(decision)

        detector_decisions = [
            d for d, _ in await asyncio.gather(*(self._run(b, stage, current, ctx) for b in detectors))
        ]
        decisions.extend(detector_decisions)

        spans = [
            span
            for d in detector_decisions
            if d.mode is Mode.ENFORCE and d.action is Action.REDACT
            for span in d.spans
        ]
        if spans:
            current = redact(current, spans)

        result = GuardResult(
            stage=stage,
            action=strongest([d.action for d in decisions]),
            would_action=strongest([d.would_action for d in decisions]),
            text=current,
            decisions=decisions,
            config_hash=self._config_hash,
            latency_ms=(time.perf_counter() - started) * 1000,
            pending_async=[b.config.id for b in deferred],
        )

        await self._emit(DecisionEvent(stage, ctx, decisions, self._config_hash, is_async=False))
        if deferred:
            self._schedule(deferred, stage, current, ctx)
        return result

    async def drain(self) -> None:
        """Wait for all scheduled async policies to finish (tests, shutdown, eval runs)."""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def _run(
        self, bound: _BoundPolicy, stage: Stage, text: str, ctx: CheckContext
    ) -> tuple[PolicyDecision, Detection | None]:
        policy = bound.config
        mode = self.mode_of(policy.id)
        timeout_s = policy.timeout_ms / 1000

        detection: Detection | None = None
        error: str | None = None
        t0 = time.perf_counter()
        try:
            detection = await asyncio.wait_for(bound.detector.detect(text, ctx), timeout=timeout_s)
        except TimeoutError:
            error = f"timeout after {policy.timeout_ms}ms"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("detector error in policy %s: %s", policy.id, error)
        latency_ms = (time.perf_counter() - t0) * 1000

        reasons: list[str] = []
        if error is not None:
            if policy.on_error is OnError.FAIL_CLOSED:
                would = Action.FLAG if policy.execution is Execution.ASYNC else Action.BLOCK
            else:
                would = Action.ALLOW
            reasons.append(f"detector error ({policy.on_error.value}): {error}")
        elif detection.triggered:
            would = policy.action
            reasons.extend(detection.reasons)
            if would is Action.REDACT and not detection.spans:
                # Asked to redact but nothing to redact: refuse to pass the text through unchanged.
                would = Action.BLOCK
                reasons.append("redact requested but detector returned no spans; blocking instead")
        else:
            would = Action.ALLOW
            reasons.extend(detection.reasons)

        decision = PolicyDecision(
            policy_id=policy.id,
            policy_version=str(self.config.version),
            stage=stage,
            mode=mode,
            execution=policy.execution,
            action=would if mode is Mode.ENFORCE else Action.ALLOW,
            would_action=would,
            detector=policy.detector.type,
            score=detection.score if detection else None,
            threshold=bound.detector.threshold,
            reasons=reasons,
            spans=list(detection.spans) if detection else [],
            latency_ms=latency_ms,
            cost_usd=detection.cost_usd if detection else 0.0,
            error=error,
        )
        return decision, detection

    def _schedule(self, deferred: list[_BoundPolicy], stage: Stage, text: str, ctx: CheckContext) -> None:
        if len(self._pending) >= self.max_pending_async:
            self.dropped_async += len(deferred)
            logger.warning("async queue full; dropped %d policy checks", len(deferred))
            return
        task = asyncio.create_task(self._run_deferred(deferred, stage, text, ctx))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _run_deferred(
        self, deferred: list[_BoundPolicy], stage: Stage, text: str, ctx: CheckContext
    ) -> None:
        results = await asyncio.gather(*(self._run(b, stage, text, ctx) for b in deferred))
        decisions = [d for d, _ in results]
        await self._emit(DecisionEvent(stage, ctx, decisions, self._config_hash, is_async=True))

    async def _emit(self, event: DecisionEvent) -> None:
        for sink in self.sinks:
            try:
                await sink.record(event)
            except Exception:
                logger.exception("decision sink %r failed", sink)
