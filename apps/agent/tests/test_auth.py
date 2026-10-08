"""Login and roles: users get their own chat and nothing else; admins get the dashboard; every API path
not listed for users is admin-only."""

from __future__ import annotations

import pytest

from boundary_agent.auth import Authenticator, parse_accounts, required_role

pytestmark = pytest.mark.auth


def test_paths_default_to_admin():
    assert required_role("/health") is None
    assert required_role("/api/auth/login") is None
    assert required_role("/api/chat") == "user"
    assert required_role("/api/conversations/abc/messages") == "user"
    for path in (
        "/api/guard/scan",
        "/api/playground/attack",
        "/api/logs",
        "/api/approvals",
        "/api/new-thing",
    ):
        assert required_role(path) == "admin"
    assert required_role("/api/conversationsX") == "admin"  # no prefix tricks


def test_tokens_are_signed_and_expire():
    auth = Authenticator("ann:pw:user", "secret", 1)
    account = auth.login("ann", "pw")
    token = auth.issue(account)
    assert auth.verify(token) == account
    assert auth.login("ann", "nope") is None and auth.login("bob", "pw") is None
    payload, sig = token.split(".")
    assert auth.verify(payload + "." + sig[:-2] + "AA") is None  # tampered
    assert Authenticator("ann:pw:user", "other", 1).verify(token) is None  # other key
    auth.token_seconds = -1
    assert auth.verify(auth.issue(account)) is None  # expired


def test_bad_account_specs_are_rejected():
    with pytest.raises(ValueError):
        parse_accounts("ann:pw:superuser")


@pytest.fixture
async def api(monkeypatch):
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from boundary_agent import main, services
    from boundary_agent.db import Base, get_session

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def session():
        async with factory() as s:
            yield s

    monkeypatch.setattr(services.settings, "auth_required", True)
    monkeypatch.setattr(
        services, "authenticator", Authenticator("ann:pw1:user,bob:pw2:user,root:pw3:admin", "k", 1)
    )
    main.app.dependency_overrides[get_session] = session
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    main.app.dependency_overrides.clear()
    await engine.dispose()


async def _token(client, name, password):
    response = await client.post("/api/auth/login", json={"username": name, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def test_signed_out_requests_are_refused(api):
    assert (await api.get("/api/conversations")).status_code == 401
    assert (await api.post("/api/guard/scan", json={"stage": "user_input", "text": "x"})).status_code == 401
    assert (await api.get("/health")).status_code == 200


async def test_users_get_the_chat_only_and_admins_the_dashboard(api):
    ann = await _token(api, "ann", "pw1")
    root = await _token(api, "root", "pw3")
    assert (await api.get("/api/auth/me", headers=ann)).json() == {"username": "ann", "role": "user"}
    assert (await api.get("/api/conversations", headers=ann)).status_code == 200
    for path in ("/api/logs", "/api/approvals", "/api/guard/status", "/api/policies"):
        assert (await api.get(path, headers=ann)).status_code == 403, path
        assert (await api.get(path, headers=root)).status_code == 200, path
    # The playground (scan, attack) is an admin tool now.
    assert (await api.get("/api/playground/scenarios", headers=ann)).status_code == 403


async def test_users_only_see_their_own_conversations(api):
    ann = await _token(api, "ann", "pw1")
    bob = await _token(api, "bob", "pw2")
    created = await api.post("/api/conversations", json={"title": "ann's"}, headers=ann)
    cid = created.json()["id"]
    assert [c["id"] for c in (await api.get("/api/conversations", headers=ann)).json()] == [cid]
    assert (await api.get("/api/conversations", headers=bob)).json() == []
    assert (await api.get(f"/api/conversations/{cid}/messages", headers=bob)).status_code == 404
    reply = await api.post("/api/chat", json={"conversation_id": cid, "message": "hi"}, headers=bob)
    assert reply.status_code == 404


async def test_the_event_stream_takes_its_token_from_the_query_and_is_admin_only(api):
    ann = await _token(api, "ann", "pw1")
    token = ann["Authorization"].split()[1]
    assert (await api.get(f"/api/events/stream?access_token={token}")).status_code == 403
    # A token in the query is only read for the stream.
    assert (await api.get(f"/api/conversations?access_token={token}")).status_code == 401


async def test_repeated_failed_logins_are_throttled(api):
    for _ in range(10):
        bad = await api.post("/api/auth/login", json={"username": "ann", "password": "x"})
        assert bad.status_code == 401
    blocked = await api.post("/api/auth/login", json={"username": "ann", "password": "pw1"})
    assert blocked.status_code == 429  # even with the right password, until the 5-minute window passes


async def _login(client, name, password) -> int:
    response = await client.post("/api/auth/login", json={"username": name, "password": password})
    return response.status_code


async def test_failed_logins_are_stored_and_a_success_clears_them(api):
    from sqlalchemy import func, select

    from boundary_agent import main
    from boundary_agent.db import get_session
    from boundary_agent.models import LoginFailure

    for _ in range(9):
        assert (
            await api.post("/api/auth/login", json={"username": "ann", "password": "x"})
        ).status_code == 401
    # Kept in the database (not process memory), so a restart doesn't reset the count.
    async for session in main.app.dependency_overrides[get_session]():
        assert await session.scalar(select(func.count()).select_from(LoginFailure)) == 9
    assert await _login(api, "ann", "pw1") == 200
    for _ in range(9):  # the count started again from zero
        assert (
            await api.post("/api/auth/login", json={"username": "ann", "password": "x"})
        ).status_code == 401


async def test_chat_with_an_unknown_conversation_is_a_404(api):
    ann = await _token(api, "ann", "pw1")
    reply = await api.post("/api/chat", json={"conversation_id": "no-such-id", "message": "hi"}, headers=ann)
    assert reply.status_code == 404
    assert (await api.get("/api/conversations", headers=ann)).json() == []  # nothing was created
