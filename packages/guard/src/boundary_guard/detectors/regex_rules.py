from __future__ import annotations

import asyncio
import base64
import binascii
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from boundary_guard.core.detector import Detector, file_digest, register_detector
from boundary_guard.core.types import CheckContext, Detection, Span

# Above this size, matching runs in a worker thread so a slow pattern can't stall the event loop
# (the policy timeout then still fires, even though the thread itself can't be cancelled).
_THREAD_THRESHOLD_CHARS = 20_000

_FLAG_NAMES = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}


def decodes_to_text(value: str) -> bool:
    """True if `value` is base64 (standard or URL-safe) of readable text.

    Issued keys are random bytes, which almost never decode to valid UTF-8, let alone prose; an
    encoded sentence (say, an instruction hidden in a page) does. Rules that match "any long random
    token" use this to tell the two apart.
    """
    padded = value + "=" * (-len(value) % 4)
    for altchars in (None, b"-_"):  # standard, then URL-safe; validate so stray characters fail
        try:
            text = base64.b64decode(padded, altchars=altchars, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
        if len(text) >= 12 and " " in text and sum(c.isprintable() for c in text) / len(text) >= 0.95:
            return True
    return False


def shannon_entropy(value: str) -> float:
    """Bits per character."""
    if not value:
        return 0.0
    counts = Counter(value)
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


@dataclass(slots=True)
class Rule:
    id: str
    label: str
    pattern: re.Pattern[str]
    group: str | int | None
    min_entropy: float | None
    exclude_encoded_text: bool = False


class RegexRulesDetector(Detector):
    """Pattern ruleset (YAML) with optional per-rule entropy floor and a global allowlist.

    Ruleset format:
        name: secrets
        version: 1
        allowlist: ['EXAMPLE']          # regexes; a match on the captured value suppresses it
        rules:
          - id: aws_access_key_id
            label: AWS_KEY
            pattern: '\\b(?:AKIA|ASIA)[0-9A-Z]{16}\\b'
            group: 0                     # optional capture group (name or index) holding the value
            min_entropy: 3.0             # optional, bits/char of the captured value
            exclude_encoded_text: true   # optional, skip values that are base64 of readable text
            flags: [i]
    """

    type_name = "regex_rules"

    def __init__(self, ruleset_path: Path, enabled: list[str] | None = None) -> None:
        self.ruleset_path = ruleset_path
        raw = yaml.safe_load(ruleset_path.read_text(encoding="utf-8"))
        self.name: str = raw.get("name", ruleset_path.stem)
        self.allowlist = [re.compile(p) for p in raw.get("allowlist", [])]

        rules: list[Rule] = []
        for item in raw["rules"]:
            if enabled is not None and item["id"] not in enabled:
                continue
            flags = 0
            for name in item.get("flags", []):
                flags |= _FLAG_NAMES[name]
            rules.append(
                Rule(
                    id=item["id"],
                    label=item.get("label", "MATCH"),
                    pattern=re.compile(item["pattern"], flags),
                    group=item.get("group"),
                    min_entropy=item.get("min_entropy"),
                    exclude_encoded_text=bool(item.get("exclude_encoded_text", False)),
                )
            )
        if not rules:
            raise ValueError(f"ruleset {ruleset_path} has no enabled rules")
        self.rules = rules
        self.enabled = enabled

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        if len(text) > _THREAD_THRESHOLD_CHARS:
            return await asyncio.to_thread(self._scan, text)
        return self._scan(text)

    def _scan(self, text: str) -> Detection:
        spans: list[Span] = []
        matched: list[str] = []
        for rule in self.rules:
            for m in rule.pattern.finditer(text):
                group = rule.group if rule.group is not None else 0
                value = m.group(group)
                if not value:
                    continue
                if rule.min_entropy is not None and shannon_entropy(value) < rule.min_entropy:
                    continue
                if any(a.search(value) for a in self.allowlist):
                    continue
                if rule.exclude_encoded_text and decodes_to_text(value):
                    continue
                start, end = m.span(group)
                spans.append(Span(start, end, rule.label))
                if rule.id not in matched:
                    matched.append(rule.id)
        return Detection(
            triggered=bool(spans),
            score=1.0 if spans else 0.0,
            spans=spans,
            reasons=[f"matched {self.name}:{rule_id}" for rule_id in matched],
        )

    def fingerprint(self) -> str:
        return f"{file_digest(self.ruleset_path)}:{','.join(self.enabled or ['*'])}"


@register_detector("regex_rules")
def _factory(params: dict[str, Any], base_dir: Path) -> Detector:
    return RegexRulesDetector(base_dir / params["ruleset"], enabled=params.get("rules"))
