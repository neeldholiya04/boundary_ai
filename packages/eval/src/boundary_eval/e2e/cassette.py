"""Record/replay for the planner's LLM calls, so the end-to-end eval is deterministic in CI.

A cassette is a JSON file mapping a request hash to the recorded response. The hash covers the
model, messages, tools and temperature, so any change to the prompt (e.g. spotlighting on/off) is
a different key and needs its own recording.

Modes:
- live:   call the provider, record nothing.
- record: call the provider and append new responses to the cassette (existing keys are reused,
          so a re-record only fills gaps).
- replay: serve from the cassette; a miss raises, so CI never depends on the network or a key.

Installed by monkeypatching `boundary_agent.llm.litellm.acompletion` for the duration of a run, which
is exactly what the agent's planner calls.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class CassetteMiss(RuntimeError):
    pass


# Spotlighting wraps tool output in markers with a fresh random nonce per request (so injected text
# can't forge them). The nonce changes nothing about the request's meaning, so it is normalised out
# of the key; otherwise no spotlighted run could ever replay.
_NONCE = re.compile(r"(<<(?:end_)?untrusted_tool_output) [0-9a-f]+>>")


def _strip_nonces(value: Any) -> Any:
    if isinstance(value, str):
        return _NONCE.sub(r"\1 NONCE>>", value)
    if isinstance(value, list):
        return [_strip_nonces(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_nonces(v) for k, v in value.items()}
    return value


def _key(kwargs: dict[str, Any]) -> str:
    payload = {
        "model": kwargs.get("model"),
        "messages": _strip_nonces(kwargs.get("messages")),
        "tools": kwargs.get("tools"),
        "temperature": kwargs.get("temperature"),
        "tool_choice": kwargs.get("tool_choice"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class Cassette:
    def __init__(self, path: Path, mode: str = "replay") -> None:
        if mode not in ("live", "record", "replay"):
            raise ValueError(f"mode must be live/record/replay, not {mode!r}")
        self.path = path
        self.mode = mode
        self.entries: dict[str, Any] = {}
        if path.exists():
            self.entries = json.loads(path.read_text(encoding="utf-8"))
        self.hits = 0
        self.misses = 0
        self.recorded = 0

    async def acompletion(self, real_acompletion: Any, **kwargs: Any) -> Any:
        from litellm import ModelResponse

        key = _key(kwargs)
        if key in self.entries and self.mode != "live":
            self.hits += 1
            return ModelResponse(**self.entries[key])

        if self.mode == "replay":
            self.misses += 1
            raise CassetteMiss(
                f"no recorded response for model={kwargs.get('model')} (key {key[:12]}). "
                "Record the cassette with a real key: boundary-eval e2e --record."
            )

        response = await real_acompletion(**kwargs)
        if self.mode == "record":
            self.entries[key] = response.model_dump(mode="json")
            self.recorded += 1
        return response

    def save(self) -> None:
        if self.mode in ("record",) and self.recorded:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            ordered = dict(sorted(self.entries.items()))
            self.path.write_text(json.dumps(ordered, indent=2, sort_keys=False) + "\n", encoding="utf-8")


@contextmanager
def install(cassette: Cassette):
    """Route the planner's LLM calls through the cassette for the duration of the block."""
    from boundary_agent import llm

    real = llm.litellm.acompletion

    async def wrapper(**kwargs: Any) -> Any:
        return await cassette.acompletion(real, **kwargs)

    llm.litellm.acompletion = wrapper
    try:
        yield cassette
    finally:
        llm.litellm.acompletion = real
        cassette.save()
