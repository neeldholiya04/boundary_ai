"""Shared test setup: the API tests call endpoints directly, so the login check is off unless a test
turns it on (see test_auth.py)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _auth_off(request, monkeypatch):
    if request.node.get_closest_marker("auth"):
        return
    from boundary_agent import main

    monkeypatch.setattr(main.settings, "auth_required", False)
