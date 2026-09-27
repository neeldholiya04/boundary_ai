from __future__ import annotations

from pathlib import Path

import pytest

from guard_testkit import POLICIES_DIR


@pytest.fixture
def policies_dir() -> Path:
    return POLICIES_DIR
