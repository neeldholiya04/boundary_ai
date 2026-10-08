"""`all_of`: fire only when every member detector fires (a second opinion before acting).

    detector:
      type: all_of
      detectors:
        - {type: hf_classifier, model: ..., threshold: 0.5}   # primary: its score is the policy's
        - {type: hf_classifier, model: ..., threshold: 0.5}   # confirmer
      primary_alone: 0.999     # optional: the primary alone decides at or above this score

Two models that fail differently cut false alarms where either alone is noisy: Llama Prompt Guard
rates "write a note on why the above chats were blocked" 0.998 jailbreak, ProtectAI rates it 0.24;
ProtectAI flags "ok continue", Prompt Guard doesn't. Members run concurrently. A confirmer that
errors doesn't decide anything: the primary's verdict stands, so a busy or broken second model can't
open (or close) the gate. The policy has no single threshold (`threshold` is None), so tuning tools
leave it alone; the primary's score is still reported.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from boundary_guard.core.detector import Detector, build_detector, register_detector
from boundary_guard.core.types import CheckContext, Detection


class AllOfDetector(Detector):
    type_name = "all_of"

    def __init__(self, members: list[Detector], primary_alone: float | None = None) -> None:
        if len(members) < 2:
            raise ValueError("all_of needs at least two detectors")
        self.members = members
        self.primary_alone = primary_alone
        self.threshold = None

    def applies(self, ctx: CheckContext) -> bool:
        return all(m.applies(ctx) for m in self.members)

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        primary, *others = await asyncio.gather(
            *(m.detect(text, ctx) for m in self.members), return_exceptions=True
        )
        if isinstance(primary, BaseException):
            raise primary  # the policy's on_error applies, as for a single detector
        reasons = list(primary.reasons)
        confirmed = True
        for member, result in zip(self.members[1:], others, strict=True):
            if isinstance(result, BaseException):
                reasons.append(f"{member.type_name} errored ({type(result).__name__}); primary decides")
                continue
            confirmed = confirmed and result.triggered
            verdict = "confirmed by" if result.triggered else "not confirmed by"
            detail = f" ({result.reasons[0]})" if result.reasons else ""
            reasons.append(f"{verdict} {member.type_name}{detail}")
        alone = (
            self.primary_alone is not None
            and primary.score is not None
            and primary.score >= self.primary_alone
        )
        if alone:
            reasons.append(f"primary score >= {self.primary_alone}: decides alone")
        triggered = primary.triggered and (confirmed or alone)
        costs = [r.cost_usd for r in (primary, *others) if not isinstance(r, BaseException)]
        return Detection(
            triggered=triggered,
            score=primary.score,
            spans=primary.spans if triggered else [],
            reasons=reasons,
            cost_usd=sum(costs),
        )

    def fingerprint(self) -> str:
        members = "|".join(f"{m.type_name}:{m.fingerprint()}" for m in self.members)
        return f"all_of({members}):alone={self.primary_alone}"


@register_detector("all_of")
def _factory(params: dict[str, Any], base_dir: Path) -> Detector:
    members = []
    for spec in params["detectors"]:
        spec = dict(spec)
        members.append(build_detector(spec.pop("type"), spec, base_dir))
    alone = params.get("primary_alone")
    return AllOfDetector(members, primary_alone=float(alone) if alone is not None else None)
