"""The agent's HTTP app: sign-in middleware, CORS and the API routers (boundary_agent/api/). Runtime
objects live in services.py; start-up and shutdown in startup.py. Run with `boundary serve`."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from boundary_agent import services
from boundary_agent.api import approvals, auth, chat, guard, guard_rules, logs, system, tool_policies, tools
from boundary_agent.config import REPO_ROOT
from boundary_agent.playground import build_router as build_playground_router
from boundary_agent.startup import lifespan

settings = services.settings


def _origins(origin: str) -> list[str]:
    """The configured dashboard origin plus its localhost/127.0.0.1 twin (browsers treat them apart)."""
    twins = {
        origin,
        origin.replace("://localhost", "://127.0.0.1"),
        origin.replace("://127.0.0.1", "://localhost"),
    }
    return sorted(twins)


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# Order matters: the sign-in check is added first, so CORS (added after) wraps it and its 401/403
# responses still carry the CORS headers the dashboard needs to read them.
app.middleware("http")(auth.require_login)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins(settings.frontend_origin),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (
    system.router,
    auth.router,
    chat.router,
    tools.router,
    tool_policies.router,
    approvals.router,
    guard.router,
    guard_rules.router,
    logs.router,
):
    app.include_router(router)
app.include_router(
    build_playground_router(
        settings=settings,
        guard=services.guard,
        telemetry=services.telemetry,
        spend=services.spend,
        limiter=services.limiter,
        repo_root=REPO_ROOT,
    )
)
