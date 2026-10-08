"""Sign-in: the middleware that checks every /api request's role, and /api/auth/*. Roles and tokens:
see boundary_agent/auth.py."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from boundary_agent import services
from boundary_agent.auth import current_account, required_role, token_from
from boundary_agent.schemas import LoginRequest

router = APIRouter(prefix="/api/auth")

# Failed sign-ins per client: 10 per 5 minutes, then wait.
LOGIN_WINDOW_S = 300
LOGIN_MAX_FAILURES = 10
login_failures: dict[str, list[float]] = {}


async def require_login(request: Request, call_next):
    """Every /api path needs the role auth.required_role gives it (default: admin). Registered before
    CORS, so CORS wraps it and a 401 still carries the headers the dashboard needs to read it."""
    role = required_role(request.url.path)
    if request.method == "OPTIONS" or role is None or not services.settings.auth_required:
        return await call_next(request)
    token = token_from(request)
    account = services.authenticator.verify(token) if token else None
    if account is None:
        return JSONResponse({"detail": "Sign in first."}, status_code=401)
    if role == "admin" and account.role != "admin":
        return JSONResponse({"detail": "This needs an admin account."}, status_code=403)
    request.state.account = account
    return await call_next(request)


@router.post("/login")
async def login(payload: LoginRequest, request: Request) -> dict:
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    recent = [t for t in login_failures.get(client, []) if now - t < LOGIN_WINDOW_S]
    if len(recent) >= LOGIN_MAX_FAILURES:
        raise HTTPException(status_code=429, detail="Too many failed sign-ins. Try again in a few minutes.")
    account = services.authenticator.login(payload.username.strip(), payload.password)
    if account is None:
        login_failures[client] = [*recent, now]
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    login_failures.pop(client, None)
    return {
        "token": services.authenticator.issue(account),
        "username": account.username,
        "role": account.role,
    }


@router.get("/me")
async def me(request: Request) -> dict:
    account = current_account(request)
    if account is None:  # auth switched off (tests, local tools)
        return {"username": "local", "role": "admin"}
    return {"username": account.username, "role": account.role}
