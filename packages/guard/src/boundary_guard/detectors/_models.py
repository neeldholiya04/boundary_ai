"""Shared loading for ML detectors.

Heavy libraries (torch, transformers, presidio) are imported inside functions so that
`import boundary_guard` stays light for regex/schema-only users. Each model is loaded once
per process and shared by every policy that uses it, keyed by (kind, name, revision).
Inference on a shared model is serialised with a per-model lock; detectors call it from a
worker thread so the event loop keeps running.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_registry_lock = threading.Lock()
_registry: dict[tuple[str, ...], LoadedModel] = {}


@dataclass(slots=True)
class LoadedModel:
    obj: Any
    lock: threading.Lock = field(default_factory=threading.Lock)


def shared(key: tuple[str, ...], factory: Callable[[], Any]) -> LoadedModel:
    with _registry_lock:
        loaded = _registry.get(key)
        if loaded is None:
            loaded = LoadedModel(factory())
            _registry[key] = loaded
        return loaded


def loaded_keys() -> list[tuple[str, ...]]:
    with _registry_lock:
        return list(_registry)


def require_ml() -> None:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "ML detectors need the optional dependencies: pip install 'boundary-guard[ml]'"
        ) from exc


def torch_device() -> str:
    # CPU only for now: deterministic, and what the free-tier hosts in PLAN.md provide.
    return "cpu"
