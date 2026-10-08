"""Login and roles.

Two roles: `user` (the chat, and only their own conversations) and `admin` (the dashboard: guardrails,
approvals, logs, tools, playground). Every /api path is mapped to the role it needs in `required_role`;
anything not listed needs `admin`, so a new endpoint is private until someone opens it on purpose.

Accounts come from AUTH_USERS ("name:password:role,…"). The defaults (admin/admin123, user/user123) are
for local use; deployments set their own. A login returns a signed, expiring token (HMAC-SHA256 with
AUTH_SECRET); the dashboard sends it as `Authorization: Bearer …`, or `?access_token=` for the event
stream, which browsers can't give headers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Literal

from fastapi import Request

logger = logging.getLogger("boundary.auth")

Role = Literal["user", "admin"]

# Paths anyone may call (no token).
PUBLIC_PATHS = {"/health", "/api/auth/login"}
# Paths a `user` may call: their chat. `/api/conversations/{id}/…` is checked for ownership in the
# handlers. Everything else under /api is admin-only.
USER_PATHS = ("/api/auth/me", "/api/chat", "/api/conversations")


@dataclass(frozen=True, slots=True)
class Account:
    username: str
    password: str
    role: Role


def parse_accounts(spec: str) -> dict[str, Account]:
    accounts: dict[str, Account] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, password, role = (part.strip() for part in item.split(":", 2))
        if role not in ("user", "admin") or not name or not password:
            raise ValueError(f"AUTH_USERS entry for {name!r}: need name:password:user|admin")
        accounts[name] = Account(name, password, role)  # type: ignore[arg-type]
    return accounts


def required_role(path: str) -> Role | None:
    """The role a path needs: None (public), "user" (any signed-in account) or "admin"."""
    if path in PUBLIC_PATHS or not path.startswith("/api/"):
        return None
    if any(path == p or path.startswith(p + "/") for p in USER_PATHS):
        return "user"
    return "admin"


class Authenticator:
    def __init__(self, users_spec: str, secret: str | None, token_hours: int) -> None:
        self.accounts = parse_accounts(users_spec)
        if not secret:
            # Tokens then last until the process restarts; set AUTH_SECRET for anything shared or
            # run with several workers.
            secret = secrets.token_urlsafe(32)
            logger.warning("AUTH_SECRET is not set: using a random one; logins end on restart")
        self._key = secret.encode()
        self.token_seconds = token_hours * 3600

    def login(self, username: str, password: str) -> Account | None:
        account = self.accounts.get(username)
        # Compare even for unknown names, so timing doesn't tell which usernames exist.
        expected = account.password if account else secrets.token_urlsafe(16)
        ok = hmac.compare_digest(password.encode(), expected.encode())
        return account if (account and ok) else None

    def issue(self, account: Account) -> str:
        body = json.dumps(
            {"u": account.username, "r": account.role, "exp": int(time.time()) + self.token_seconds},
            separators=(",", ":"),
        ).encode()
        payload = base64.urlsafe_b64encode(body).decode().rstrip("=")
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str) -> Account | None:
        try:
            payload, signature = token.split(".", 1)
            if not hmac.compare_digest(signature, self._sign(payload)):
                return None
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, json.JSONDecodeError):
            return None
        if data.get("exp", 0) < time.time():
            return None
        account = self.accounts.get(data.get("u", ""))
        # The account must still exist with the same role: removing someone from AUTH_USERS (and
        # restarting) ends their sessions.
        if account is None or account.role != data.get("r"):
            return None
        return account

    def _sign(self, payload: str) -> str:
        digest = hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def token_from(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # EventSource can't send headers; only the event stream reads the token from the query.
    if request.url.path == "/api/events/stream":
        return request.query_params.get("access_token")
    return None


def current_account(request: Request) -> Account | None:
    """The signed-in account for this request (set by the auth middleware), or None when auth is off."""
    return getattr(request.state, "account", None)
