from __future__ import annotations

import pytest
from pydantic import ValidationError

from boundary_guard import Execution, Guard, Mode, OnError
from guard_testkit import make_config, policy


def test_defaults_are_resolved_onto_policies():
    config = make_config(policy("a"), timeout_ms=123, on_error="fail_open", mode="shadow")
    p = config.policies[0]
    assert p.timeout_ms == 123
    assert p.on_error is OnError.FAIL_OPEN
    assert p.mode is Mode.SHADOW
    assert p.execution is Execution.BLOCKING


def test_policy_values_override_defaults():
    config = make_config(policy("a", mode="enforce", timeout_ms=50), mode="shadow")
    assert config.policies[0].mode is Mode.ENFORCE
    assert config.policies[0].timeout_ms == 50


def test_duplicate_policy_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate policy id"):
        make_config(policy("a"), policy("a"))


def test_async_policy_must_flag():
    with pytest.raises(ValidationError, match="async policies can only flag"):
        make_config(policy("a", action="block", stages=["final_output"], execution="async"))


@pytest.mark.parametrize("stage", ["tool_output", "tool_args"])
def test_async_rejected_where_content_is_consumed_immediately(stage):
    with pytest.raises(ValidationError, match="require blocking execution"):
        make_config(policy("a", action="flag", stages=[stage], execution="async"))


def test_invalid_policy_id_rejected():
    with pytest.raises(ValidationError):
        make_config(policy("Bad-Id"))


def test_unknown_detector_type_fails_at_guard_build():
    config = make_config(policy("a", type="does_not_exist"))
    with pytest.raises(ValueError, match="unknown detector type"):
        Guard(config)


def test_config_hash_is_stable_and_sensitive_to_changes():
    a1 = Guard(make_config(policy("a", triggered=False)))
    a2 = Guard(make_config(policy("a", triggered=False)))
    b = Guard(make_config(policy("a", triggered=True)))
    assert a1.config_hash == a2.config_hash
    assert a1.config_hash != b.config_hash


def test_shipped_policy_file_loads(policies_dir):
    guard = Guard.from_yaml(policies_dir / "guard.yaml")
    assert "secrets" in guard.policy_ids
    assert len(guard.config_hash) == 16
