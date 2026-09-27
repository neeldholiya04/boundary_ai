"""Shared helpers for guard tests (on pytest's pythonpath). Importing registers stub detectors."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from boundary_guard import CheckContext, Detection, Detector, GuardConfig, Span, register_detector

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICIES_DIR = REPO_ROOT / "policies"


class StubDetector(Detector):
    """Configurable detector for pipeline tests."""

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params
        self.threshold = params.get("threshold")
        self.seen: list[str] = []

    def applies(self, ctx: CheckContext) -> bool:
        return self.params.get("applies", True)

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        self.seen.append(text)
        if delay := self.params.get("delay_ms"):
            await asyncio.sleep(delay / 1000)
        if self.params.get("raise"):
            raise RuntimeError("boom")
        spans = [Span(s, e, label) for s, e, label in self.params.get("spans", [])]
        if self.params.get("match"):
            needle = self.params["match"]
            idx = text.find(needle)
            spans = [Span(idx, idx + len(needle), self.params.get("label", "X"))] if idx >= 0 else []
            triggered = idx >= 0
        else:
            triggered = self.params.get("triggered", False)
        return Detection(
            triggered=triggered,
            score=self.params.get("score"),
            spans=spans,
            reasons=list(self.params.get("reasons", [])),
            cost_usd=self.params.get("cost_usd", 0.0),
            rewrite=self.params.get("rewrite"),
        )


class StubTransform(StubDetector):
    transform = True


register_detector("stub")(lambda params, base_dir: StubDetector(params))
register_detector("stub_transform")(lambda params, base_dir: StubTransform(params))


def make_config(*policies: dict[str, Any], version: int = 1, **defaults: Any) -> GuardConfig:
    return GuardConfig.model_validate({"version": version, "defaults": defaults, "policies": list(policies)})


def policy(
    pid: str, action: str = "block", stages: list[str] | None = None, **detector: Any
) -> dict[str, Any]:
    extra = {k: detector.pop(k) for k in ("mode", "execution", "timeout_ms", "on_error") if k in detector}
    det_type = detector.pop("type", "stub")
    return {
        "id": pid,
        "stages": stages or ["user_input"],
        "action": action,
        "detector": {"type": det_type, **detector},
        **extra,
    }
