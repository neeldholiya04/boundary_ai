import argparse
import os
from unittest import mock

import pytest

from boundary_agent import cli
from boundary_agent.config import get_settings


@pytest.fixture(autouse=True)
def isolated_env():
    # The CLI writes flags into os.environ and Settings is cached; undo both after each test.
    with mock.patch.dict(os.environ, {}, clear=False):
        for key in ("DATABASE_URL", "REDIS_URL", "GUARD_POLICY_PATH", "LLM_PROVIDER"):
            os.environ.pop(key, None)
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


def test_demo_runs_without_keys_and_keeps_the_guard():
    cli._apply_overrides(argparse.Namespace(demo=True, no_guard=False, policy=None))
    assert os.environ["LLM_PROVIDER"] == "mock"
    assert os.environ["ALLOW_DEMO_MOCK_PLANNER"] == "true"
    assert os.environ["DATABASE_URL"].startswith("sqlite")
    assert os.environ["GUARD_POLICY_PATH"] == "policies/guard.yaml"


def test_explicit_env_wins_over_demo_defaults():
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./mine.db"
    cli._apply_overrides(argparse.Namespace(demo=True, no_guard=False, policy=None))
    assert os.environ["DATABASE_URL"] == "sqlite+aiosqlite:///./mine.db"


def test_no_guard_clears_the_policy(capsys):
    cli.main(["status", "--demo", "--no-guard"])
    assert "guard         off" in capsys.readouterr().out


def test_status_lists_policies_without_loading_models(capsys):
    cli.main(["status", "--demo"])
    out = capsys.readouterr().out
    assert "planner       mock" in out
    assert "policies/guard.yaml" in out
    assert "user_injection_promptguard" in out


def test_serve_and_status_share_flags():
    with pytest.raises(SystemExit):
        cli.main(["serve", "--policy", "x.yaml", "--no-guard"])  # mutually exclusive


def test_status_never_prints_database_passwords(capsys):
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://boundary:s3cret-value@localhost:5433/boundary"
    os.environ["REDIS_URL"] = "redis://:redis-pass@localhost:6379/0"
    cli.main(["status", "--no-guard"])
    out = capsys.readouterr().out
    assert "s3cret-value" not in out and "redis-pass" not in out
    assert "boundary:***@localhost:5433/boundary" in out
