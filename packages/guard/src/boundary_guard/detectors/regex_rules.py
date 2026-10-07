from __future__ import annotations

import asyncio
import base64
import binascii
import fnmatch
import json
import logging
import math
import os
import re
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from boundary_guard.core.detector import Detector, file_digest, register_detector
from boundary_guard.core.redact import redact
from boundary_guard.core.types import CheckContext, Detection, Span

# Above this size, matching runs in a worker thread so a slow pattern can't stall the event loop
# (the policy timeout then still fires, even though the thread itself can't be cancelled).
_THREAD_THRESHOLD_CHARS = 20_000

_FLAG_NAMES = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}


def b64_text(value: str, *, min_printable: float = 0.9) -> str | None:
    """Decode base64 (standard or URL-safe, padding optional) to readable UTF-8 text, or None."""
    padded = value + "=" * (-len(value) % 4)
    for altchars in (None, b"-_"):  # standard, then URL-safe; validate so stray characters fail
        try:
            text = base64.b64decode(padded, altchars=altchars, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
        if text and sum(c.isprintable() or c in "\n\r\t" for c in text) / len(text) >= min_printable:
            return text
    return None


def decodes_to_text(value: str) -> bool:
    """True if `value` is base64 of readable prose (12+ chars with a space).

    Issued keys are random bytes, which almost never decode to valid UTF-8, let alone prose; an
    encoded sentence (say, an instruction hidden in a page) does. Rules that match "any long random
    token" use this to tell the two apart.
    """
    text = b64_text(value, min_printable=0.95)
    return text is not None and len(text) >= 12 and " " in text


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


# Decoded views (`decode:` in the policy): a key hidden as base64 or spelled out with spaces. The
# whole encoded run is the span, so redaction removes the encoding along with the key inside it.
# Base64 may follow `=` or `:` (`ENV_B64=<blob>`), never another base64 character.
_B64_CANDIDATE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{24,}={0,2}(?![A-Za-z0-9+/=_-])")
# Single key-alphabet characters separated by single spaces or tabs, from a word edge (`token=s k - p r
# o j …` counts). Quotes, colons and pipes are not run characters, so JSON and tables stay intact.
_SPACED_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z0-9_+/=.-][ \t]){15,}[A-Za-z0-9_+/=.-](?![A-Za-z0-9])"
)
# Bound the work per check by decoded text, not candidate count: tool output routinely holds hundreds of
# UUIDs and hashes, and a count cap would let them push a real key out of reach. Past the budget the
# check fails closed (no spans: a redact policy blocks), since the rest of the text went unscanned.
_DECODE_BUDGET_CHARS = 256_000
_MAX_DECODE_DEPTH = 2  # base64 of base64
DECODERS = ("base64", "spaced")
# Known secrets shorter than this, or low in entropy (`boundary-local`, `changeme-please`), would
# redact ordinary words and template defaults.
_MIN_KNOWN_SECRET_CHARS = 12
_MIN_KNOWN_SECRET_ENTROPY = 3.5
# Env vars meant to be public (bundled into frontends), whatever their name says.
_PUBLIC_ENV_PREFIXES = ("NEXT_PUBLIC_", "PUBLIC_", "VITE_", "REACT_APP_")
# Callers that must never learn whether a guess equals a deployment secret (the public playground):
# known-secret matching is skipped for checks they make.
UNTRUSTED_SOURCES = frozenset({"playground"})

logger = logging.getLogger("boundary_guard")


def known_secret_values(env_patterns: list[str], environ: dict[str, str] | None = None) -> list[str]:
    """Values of environment variables whose names match `env_patterns` (fnmatch) and look like
    credentials. Read once and held in memory only: never logged, put in reasons or the fingerprint.
    Skipped values are logged by name, so a missing protection is visible."""
    env = os.environ if environ is None else environ
    values: set[str] = set()
    skipped: list[str] = []
    for name, raw in env.items():
        if not raw or not any(fnmatch.fnmatchcase(name, p) for p in env_patterns):
            continue
        value = raw.strip()
        if name.startswith(_PUBLIC_ENV_PREFIXES):
            continue
        if len(value) < _MIN_KNOWN_SECRET_CHARS or shannon_entropy(value) < _MIN_KNOWN_SECRET_ENTROPY:
            skipped.append(name)
            continue
        values.add(value)
    if skipped:
        logger.info(
            "known secrets: not matching %s (too short or low-entropy to match safely)", sorted(skipped)
        )
    return sorted(values, key=len, reverse=True)


def _known_forms(value: str) -> list[str]:
    """The value as it appears raw, URL-encoded and JSON-escaped."""
    forms = {value, urllib.parse.quote(value, safe=""), json.dumps(value)[1:-1]}
    return sorted(forms, key=len, reverse=True)


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

    Policy params beyond the ruleset:
        decode: [base64, spaced]         # also scan decoded views; a hit spans the whole encoded run
        known_secrets_env: ['*_API_KEY'] # env var names whose values must never appear, in any format
        redact_first: true               # run before the stage's other policies and hand them the
                                         # redacted text, so no other detector (a toxicity model, an
                                         # LLM judge) ever sees the raw match
    """

    type_name = "regex_rules"

    def __init__(
        self,
        ruleset_path: Path,
        enabled: list[str] | None = None,
        *,
        decode: list[str] | None = None,
        known_secrets_env: list[str] | None = None,
        environ: dict[str, str] | None = None,
        redact_first: bool = False,
    ) -> None:
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
        unknown = set(decode or []) - set(DECODERS)
        if unknown:
            raise ValueError(f"unknown decoder(s) {sorted(unknown)} (known: {', '.join(DECODERS)})")
        self.decode = sorted(decode or [])
        self.known_secrets_env = sorted(known_secrets_env or [])
        known = known_secret_values(self.known_secrets_env, environ) if known_secrets_env else []
        self._known = [form for value in known for form in _known_forms(value)]
        # As a transform the pipeline runs this first and, when the policy enforces a redaction, passes
        # the rewritten text on to every other policy at the stage.
        self.transform = redact_first

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        known = ctx.metadata.get("source") not in UNTRUSTED_SOURCES
        if len(text) > _THREAD_THRESHOLD_CHARS:
            detection = await asyncio.to_thread(self._scan, text, known=known)
        else:
            detection = self._scan(text, known=known)
        if self.transform and detection.spans:
            detection.rewrite = redact(text, detection.spans)
        return detection

    def _match(self, text: str, *, known: bool = True) -> list[tuple[Span, str]]:
        """Rule and known-secret hits on one view of the text, as (span, reason) pairs."""
        hits: list[tuple[Span, str]] = []
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
                hits.append((Span(start, end, rule.label), f"matched {self.name}:{rule.id}"))
        for value in self._known if known else ():
            start = text.find(value)
            while start >= 0:
                end = start + len(value)
                # Whole tokens only: a secret that is also part of a longer word is left alone there.
                before_ok = start == 0 or not text[start - 1].isalnum()
                if before_ok and (end == len(text) or not text[end].isalnum()):
                    hits.append((Span(start, end, "KNOWN_SECRET"), "matched a known secret"))
                start = text.find(value, end)
        return hits

    def _scan(self, text: str, *, known: bool = True) -> Detection:
        budget = [_DECODE_BUDGET_CHARS]
        hits = self._views(text, known=known, depth=0, budget=budget)
        reasons: list[str] = []
        for _, reason in hits:
            if reason not in reasons:
                reasons.append(reason)
        if budget[0] < 0:
            reasons.append(
                f"decoding budget of {_DECODE_BUDGET_CHARS} chars exceeded; the rest was not scanned"
            )
        spans = [span for span, _ in hits]
        return Detection(
            triggered=bool(spans) or budget[0] < 0,
            score=1.0 if spans or budget[0] < 0 else 0.0,
            spans=spans,
            reasons=reasons,
        )

    def _views(self, text: str, *, known: bool, depth: int, budget: list[int]) -> list[tuple[Span, str]]:
        """Matches on the text itself plus, per `decode`, on its decoded runs (recursively, so base64
        of base64 is found). A decoded hit is reported on the encoded run in this text."""
        hits = self._match(text, known=known)
        if depth >= _MAX_DECODE_DEPTH or budget[0] < 0:
            return hits
        if "base64" in self.decode:
            for m in _B64_CANDIDATE.finditer(text):
                decoded = b64_text(m.group(0))
                if decoded is None:
                    continue
                budget[0] -= len(decoded)
                if budget[0] < 0:
                    break
                inner = self._views(decoded, known=known, depth=depth + 1, budget=budget)
                if inner:
                    hits.append((Span(m.start(), m.end(), inner[0][0].label), f"{inner[0][1]} (in base64)"))
        if "spaced" in self.decode and budget[0] >= 0:
            for m in _SPACED_CANDIDATE.finditer(text):
                run = m.group(0)
                # Trim to the first and last letter or digit: a `- - -` lead-in isn't part of a key.
                alnum = [i for i, c in enumerate(run) if c.isalnum()]
                if not alnum:
                    continue
                lead, tail = alnum[0], alnum[-1] + 1
                collapsed = re.sub(r"\s+", "", run[lead:tail])
                budget[0] -= len(collapsed)
                if budget[0] < 0:
                    break
                inner = self._match(collapsed, known=known)
                if inner:
                    span = Span(m.start() + lead, m.start() + tail, inner[0][0].label)
                    hits.append((span, f"{inner[0][1]} (spaced out)"))
        return hits

    def fingerprint(self) -> str:
        # The env var patterns, never the values: the config hash must not depend on (or leak) a key.
        return (
            f"{file_digest(self.ruleset_path)}:{','.join(self.enabled or ['*'])}"
            f":decode={','.join(self.decode)}:known={','.join(self.known_secrets_env)}"
            f":first={self.transform}"
        )


@register_detector("regex_rules")
def _factory(params: dict[str, Any], base_dir: Path) -> Detector:
    return RegexRulesDetector(
        base_dir / params["ruleset"],
        enabled=params.get("rules"),
        decode=params.get("decode"),
        known_secrets_env=params.get("known_secrets_env"),
        redact_first=bool(params.get("redact_first", False)),
    )
