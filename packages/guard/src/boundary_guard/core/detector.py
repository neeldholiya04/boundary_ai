from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from boundary_guard.core.types import CheckContext, Detection


class Detector:
    """Base class for detectors.

    A detector is built once per policy from its YAML params and reused across checks.
    `transform` detectors (e.g. schema repair) run sequentially before the others and may
    rewrite the text; plain detectors run concurrently on the (possibly rewritten) text.
    """

    type_name: ClassVar[str] = ""
    transform: ClassVar[bool] = False

    threshold: float | None = None

    def applies(self, ctx: CheckContext) -> bool:
        """Return False to skip this detector for a given check (no decision is recorded)."""
        return True

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        raise NotImplementedError

    def fingerprint(self) -> str:
        """Stable identity of everything that changes this detector's behaviour.

        Folded into the guard's config hash, so it should cover external files
        (rulesets, schemas) and model revisions, not just YAML params.
        """
        return ""


DetectorFactory = Callable[[dict[str, Any], Path], Detector]

_REGISTRY: dict[str, DetectorFactory] = {}


def register_detector(type_name: str) -> Callable[[DetectorFactory], DetectorFactory]:
    def decorator(factory: DetectorFactory) -> DetectorFactory:
        if type_name in _REGISTRY:
            raise ValueError(f"detector type already registered: {type_name}")
        _REGISTRY[type_name] = factory
        return factory

    return decorator


def build_detector(type_name: str, params: dict[str, Any], base_dir: Path) -> Detector:
    try:
        factory = _REGISTRY[type_name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise ValueError(f"unknown detector type {type_name!r} (known: {known})") from None
    return factory(params, base_dir)


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
