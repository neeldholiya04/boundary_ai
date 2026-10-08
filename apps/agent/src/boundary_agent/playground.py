"""Playground API (admin only; see auth.py).

    POST /api/guard/scan            run one guard stage over pasted text (no LLM, nothing stored)
    GET  /api/playground/scenarios  the built-in poisoned scenarios for attack mode
    POST /api/playground/attack     run a scenario (or a pasted page) with no defence vs guard on

Abuse controls: inputs are capped at PLAYGROUND_MAX_INPUT_CHARS; scans and attack runs are rate
limited per client; one attack run at a time; attack mode only ever uses the fixture tool server
(fake read-only content, in-memory "writes", no Exa, no real files); live LLM calls (pasted pages)
go through the daily budget, and built-in scenarios replay from the recorded cassette for free.

Scans are not traced or stored: they handle raw pasted text, which may be anything. They are
counted in metrics with source="playground", apart from real traffic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from boundary_agent.config import Settings
from boundary_agent.limits import BudgetExceeded, DailySpend, RateLimiter
from boundary_agent.telemetry import Telemetry
from boundary_guard import CheckContext, Guard, Stage


class ScanRequest(BaseModel):
    stage: Stage
    text: str = Field(min_length=1)


class AttackRequest(BaseModel):
    scenario_id: str | None = None
    page: str | None = Field(default=None, min_length=1)


async def scan_text(guard: Guard, stage: Stage, text: str) -> dict[str, Any]:
    # Every policy the chat runs, operator rules included: the playground is an admin tool for seeing
    # what passes and what fires. (It was public once, and then showed the shipped policies only.) The
    # deployment's own keys (known secrets) are still not checked here, so a scan can't confirm a guess.
    ctx = CheckContext(metadata={"source": "playground"})
    result = await guard.check(stage, text, ctx)
    return {
        "stage": stage.value,
        "action": result.action.value,
        "would_action": result.would_action.value,
        "text": result.text,
        "latency_ms": round(result.latency_ms, 2),
        "config_hash": result.config_hash,
        "pending_async": list(result.pending_async),
        "policies": [
            {
                "policy_id": d.policy_id,
                "mode": d.mode.value,
                "execution": d.execution.value,
                "detector": d.detector,
                "action": d.action.value,
                "would_action": d.would_action.value,
                "score": d.score,
                "threshold": d.threshold,
                "reasons": d.reasons,
                "spans": [{"start": s.start, "end": s.end, "label": s.label} for s in d.spans],
                "latency_ms": round(d.latency_ms, 2),
                "error": d.error,
            }
            for d in result.decisions
        ],
    }


def client_key(request: Request) -> str:
    # Behind a proxy, run uvicorn with --proxy-headers / --forwarded-allow-ips so this is the real
    # client address (Phase 12); never trust X-Forwarded-For from arbitrary clients here.
    return request.client.host if request.client else "unknown"


def build_router(
    *,
    settings: Settings,
    guard: Guard | None,
    telemetry: Telemetry,
    spend: DailySpend,
    limiter: RateLimiter,
    repo_root: Path,
) -> APIRouter:
    router = APIRouter(prefix="/api")
    attack_slot = asyncio.Semaphore(1)  # attack runs load the guard models twice; one at a time
    demo_telemetry = telemetry.for_demo()  # traced (tagged "playground"), kept out of the app's metrics

    def enabled() -> None:
        if not settings.playground_enabled:
            raise HTTPException(status_code=404, detail="The playground is disabled.")

    def check_size(text: str) -> None:
        limit = settings.playground_max_input_chars
        if len(text) > limit:
            raise HTTPException(
                status_code=413, detail=f"Input is {len(text)} characters; the limit is {limit}."
            )

    async def rate_limit(request: Request, bucket: str, limit: int, window_s: int) -> None:
        allowed, retry_after = await limiter.hit(bucket, client_key(request), limit=limit, window_s=window_s)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Too many {bucket} requests; try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )

    def attack_module() -> Any:
        try:
            from boundary_eval import playground
        except ImportError as exc:  # attack mode reuses the eval harness; it ships with the workspace
            raise HTTPException(
                status_code=503, detail="Attack mode needs the boundary-eval package."
            ) from exc
        return playground

    def paths(module: Any) -> Any:
        policy = settings.resolved_guard_policy_path()
        if policy is None:
            raise HTTPException(
                status_code=503, detail="Attack mode needs a guard policy (GUARD_POLICY_PATH)."
            )
        return module.Paths(repo_root=repo_root, policy=policy)

    @router.post("/guard/scan")
    async def scan(payload: ScanRequest, request: Request) -> dict[str, Any]:
        enabled()
        if guard is None:
            raise HTTPException(status_code=503, detail="The guard is off (GUARD_POLICY_PATH is empty).")
        check_size(payload.text)
        await rate_limit(request, "scan", settings.playground_scans_per_minute, 60)
        return await scan_text(guard, payload.stage, payload.text)

    @router.get("/playground/scenarios")
    async def scenarios() -> dict[str, Any]:
        enabled()
        module = attack_module()
        return {
            "scenarios": module.list_builtin(paths(module)),
            "custom_task": module.CUSTOM_TASK,
            "live_runs": settings.playground_live_runs,
            "max_input_chars": settings.playground_max_input_chars,
        }

    @router.post("/playground/attack")
    async def attack(payload: AttackRequest, request: Request) -> dict[str, Any]:
        enabled()
        if payload.page is not None:
            check_size(payload.page)
        await rate_limit(request, "attack", settings.playground_attacks_per_hour, 3600)
        module = attack_module()
        if attack_slot.locked():
            raise HTTPException(
                status_code=429,
                detail="Another attack demo is running; try again in a minute.",
                headers={"Retry-After": "30"},
            )
        async with attack_slot:
            try:
                return await module.run_attack(
                    paths(module),
                    scenario_id=payload.scenario_id,
                    page=payload.page,
                    spend=spend,
                    telemetry=demo_telemetry,
                    allow_live=settings.playground_live_runs,
                )
            except BudgetExceeded as exc:
                raise HTTPException(
                    status_code=429,
                    detail=f"{exc}. Built-in scenarios still work: they replay recorded runs for free.",
                ) from exc
            except module.PlaygroundError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
