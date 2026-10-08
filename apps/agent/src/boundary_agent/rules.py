"""Guard rules written in the dashboard: the runtime half of the guard's policies.

`policies/guard.yaml` is the fixed baseline (secrets, PII, injection, ...), reviewed and measured in CI.
A rule is what an operator adds on top, live: *where* it runs (stages, optionally specific tools),
*what* it checks (keywords, a pattern, a topic, a plain-language policy judged by an LLM, or "always"
for plain tool-call rules) and *what happens* (flag, redact, escalate to a human, block), in a mode
(off / shadow / enforce). Each rule compiles into one guard policy, `rule_<slug>`, that runs next to the
file's policies through the same pipeline, decision log, stats and config hash.

Rules carry their own examples (texts that should and shouldn't fire), and `dry_run` runs a draft
against them and against the eval set's benign records before anyone enforces it.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from boundary_guard import Action, CheckContext, Guard, GuardConfig, Mode, PolicyConfig, Stage

CheckType = Literal["keywords", "pattern", "topic", "llm_judge", "always"]
RULE_PREFIX = "rule_"
# Checks that return matched spans: only these can redact (the others can only say "this text").
SPAN_CHECKS = {"keywords", "pattern"}
# Per-check defaults for the policy timeout. A judge is a model call; the rest are local and fast.
DEFAULT_TIMEOUT_MS: dict[str, int] = {
    "keywords": 1000,
    "pattern": 2000,
    "topic": 3000,
    "llm_judge": 20000,
    "always": 1000,
}
# The topic check reuses the shipped topic model and the assistant's purpose as the allow side, so a
# rule only has to say what to deny.
TOPIC_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
TOPIC_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
TOPIC_ALLOW_FILE = "topics/research.v3.yaml"
# As the shipped topic policy (v10): a rule's deny examples must really resemble the request, so
# conversation that resembles nothing ("ok continue") isn't caught by whichever example is nearest.
TOPIC_MIN_SIMILARITY = 0.25


class RuleCheck(BaseModel):
    """What a rule looks for. Fields beyond `type` depend on the type."""

    model_config = ConfigDict(extra="forbid")

    type: CheckType
    # Sizes are bounded: a rule is compiled on the request that saves it, and topic examples are
    # embedded there too, so an unbounded list would stall the app.
    # keywords
    keywords: list[Annotated[str, Field(max_length=200)]] | None = Field(default=None, max_length=500)
    case_sensitive: bool = False
    whole_word: bool = True
    # Keywords of 7+ characters also match with one letter edit (`hemkes` for `hemkesh`).
    fuzzy: bool = False
    # pattern (the detector also caps count, length and repetition)
    patterns: list[str] | None = Field(default=None, max_length=50)
    flags: list[Literal["i", "m", "s"]] = Field(default_factory=list)
    # topic: example requests the rule is about; optional extra on-purpose examples
    examples: list[Annotated[str, Field(max_length=500)]] | None = Field(default=None, max_length=50)
    allow_examples: list[Annotated[str, Field(max_length=500)]] | None = Field(default=None, max_length=50)
    margin: float = Field(default=0.05, ge=-1.0, le=1.0)
    # llm_judge
    policy: str | None = Field(default=None, max_length=2000)
    model: str | None = Field(default=None, max_length=120)
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # Placeholder label for redactions, e.g. CODENAME -> <CODENAME_1>.
    label: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,31}$")

    @model_validator(mode="after")
    def _fields_for_type(self) -> RuleCheck:
        required = {
            "keywords": ("keywords", self.keywords),
            "pattern": ("patterns", self.patterns),
            "topic": ("examples", self.examples),
            "llm_judge": ("policy", self.policy),
        }
        if self.type in required:
            name, value = required[self.type]
            if not value or (isinstance(value, list) and not any(str(v).strip() for v in value)):
                raise ValueError(f"a {self.type} check needs `{name}`")
        return self


class RuleTests(BaseModel):
    model_config = ConfigDict(extra="forbid")

    should_fire: list[Annotated[str, Field(max_length=5000)]] = Field(default_factory=list, max_length=25)
    should_pass: list[Annotated[str, Field(max_length=5000)]] = Field(default_factory=list, max_length=25)


class RuleSpec(BaseModel):
    """A rule as the operator writes it. Stored as-is; compiled into a guard policy."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    # Shown to the user when the rule stops something. Unset: a neutral line that names no keyword.
    message: str | None = Field(default=None, max_length=300)
    stages: list[Stage] = Field(min_length=1)
    # Only these tools (tool_args / tool_output only). None = every tool.
    tools: list[Annotated[str, Field(max_length=120)]] | None = Field(
        default=None, min_length=1, max_length=50
    )
    check: RuleCheck
    action: Literal["flag", "redact", "escalate", "block"]
    # New rules start in shadow: they log what they would do until someone enforces them.
    mode: Literal["off", "shadow", "enforce"] = "shadow"
    execution: Literal["blocking", "async"] = "blocking"
    # What a check that errors (pattern over budget, judge unreachable or unreadable) does. Unset: local
    # checks fail open (a broken custom rule shouldn't take the app down), judges fail closed (text that
    # makes the judge reply nonsense must not be a way past it).
    on_error: Literal["fail_open", "fail_closed"] | None = None
    timeout_ms: int | None = Field(default=None, gt=0, le=60000)
    # Tool-output rules can mark the run as tainted when they fire, like the injection detectors. As with
    # them, this applies in shadow mode too: a shadow rule still gates later writes behind approval.
    taints_run: bool = False
    tests: RuleTests = Field(default_factory=RuleTests)

    @field_validator("stages")
    @classmethod
    def _unique_stages(cls, stages: list[Stage]) -> list[Stage]:
        return list(dict.fromkeys(stages))

    @model_validator(mode="after")
    def _consistent(self) -> RuleSpec:
        if self.action == "redact" and self.check.type not in SPAN_CHECKS:
            raise ValueError(
                f"a {self.check.type} check can't redact (it doesn't point at the text to remove); "
                "use block, escalate or flag, or a keywords/pattern check"
            )
        if self.taints_run and Stage.TOOL_OUTPUT not in self.stages:
            raise ValueError("taints_run only applies to rules that check tool output")
        return self


def policy_id_for(name: str, taken: set[str]) -> str:
    """`rule_<slug>`, unique among `taken`. Stable for a rule's life: renaming keeps the id."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "rule"
    if not slug[0].isalpha():
        slug = f"r_{slug}"
    candidate, n = f"{RULE_PREFIX}{slug}", 2
    while candidate in taken:
        candidate, n = f"{RULE_PREFIX}{slug}_{n}", n + 1
    return candidate


def detector_params(spec: RuleSpec, *, judge_model: str | None) -> dict[str, Any]:
    check = spec.check
    label = check.label or "REDACTED"
    if check.type == "keywords":
        return {
            "type": "keywords",
            "keywords": check.keywords,
            "case_sensitive": check.case_sensitive,
            "whole_word": check.whole_word,
            "fuzzy": check.fuzzy,
            "label": label,
        }
    if check.type == "pattern":
        return {"type": "pattern", "patterns": check.patterns, "flags": check.flags, "label": label}
    if check.type == "topic":
        params: dict[str, Any] = {
            "type": "embeddings_topic",
            "model": TOPIC_MODEL,
            "revision": TOPIC_REVISION,
            "deny_exemplars": check.examples,
            "margin": check.margin,
            "min_similarity": TOPIC_MIN_SIMILARITY,
            "min_words": 3,
        }
        if check.allow_examples:
            params["allow_exemplars"] = check.allow_examples
        else:
            params["allow"] = TOPIC_ALLOW_FILE
        return params
    if check.type == "llm_judge":
        return {
            "type": "llm_judge",
            "policy": check.policy,
            "model": check.model or judge_model or "",
            "threshold": check.threshold,
        }
    return {"type": "always", "reason": spec.description or f"rule: {spec.name}"}


# What a user is told when an operator rule stops them, unless the rule says otherwise. It names no
# keyword or pattern: those are often the sensitive part (codenames, people's names).
RULE_MESSAGE = "This was stopped by a rule your administrator set up."


def compile_rule(
    spec: RuleSpec,
    policy_id: str,
    *,
    judge_model: str | None = None,
    taint_labels: list[str] | None = None,
) -> PolicyConfig:
    """The guard policy a rule runs as. Validation the guard applies to every policy (async only flags,
    tools only on tool stages) happens when it is added, so callers get one place to catch errors."""
    return PolicyConfig.model_validate(
        {
            "id": policy_id,
            "description": spec.description or spec.name,
            "message": spec.message or RULE_MESSAGE,
            "stages": [s.value for s in spec.stages],
            "tools": spec.tools,
            "detector": detector_params(spec, judge_model=judge_model),
            "action": spec.action,
            # Taint follows the labels the app is configured to taint on (GUARD_TAINT_LABELS).
            "detects": list(taint_labels or ["injection"]) if spec.taints_run else [],
            "mode": spec.mode,
            "execution": spec.execution,
            "timeout_ms": spec.timeout_ms or DEFAULT_TIMEOUT_MS[spec.check.type],
            "on_error": spec.on_error or ("fail_closed" if spec.check.type == "llm_judge" else "fail_open"),
        }
    )


# ---- dry runs ------------------------------------------------------------------------------------


def _scratch_guard(policy: PolicyConfig, base_dir: Path) -> Guard:
    """A guard holding just this policy, enforced, so `would_action` is the rule's verdict."""
    enforced = policy.model_copy(update={"mode": Mode.ENFORCE})
    return Guard(GuardConfig(version=1, policies=[enforced]), base_dir=base_dir)


async def _verdict(guard: Guard, policy: PolicyConfig, text: str) -> dict[str, Any]:
    """The rule's verdict on `text` at each of its stages (as its first tool, when scoped); the first
    stage where it fires is reported. Async rules are drained, so their verdicts count too."""
    events: list[Any] = []

    class _Collect:
        async def record(self, event: Any) -> None:
            events.append(event)

    guard.sinks[:] = [_Collect()]
    ctx = CheckContext(tool_name=policy.tools[0] if policy.tools else None, metadata={"source": "rule_test"})
    first: dict[str, Any] | None = None
    for stage in policy.stages:
        events.clear()
        result = await guard.check(stage, text, ctx)
        await guard.drain()
        decisions = [d for e in events for d in e.decisions if d.policy_id == policy.id]
        decision = decisions[-1] if decisions else None
        verdict = {
            "stage": stage.value,
            "fired": decision is not None and decision.would_action is not Action.ALLOW,
            "action": decision.would_action.value if decision else "allow",
            "reasons": decision.reasons if decision else [],
            "error": decision.error if decision else None,
            "redacted": result.text if result.text != text else None,
            "latency_ms": round(decision.latency_ms, 2) if decision else 0.0,
        }
        if verdict["fired"] or verdict["error"]:
            return verdict
        first = first or verdict
    return first or {}


async def dry_run(
    spec: RuleSpec,
    *,
    base_dir: Path,
    judge_model: str | None = None,
    taint_labels: list[str] | None = None,
    benign_texts: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Run a draft rule against its own examples and (optionally) benign eval records.

    The examples are the rule's acceptance test: every `should_fire` text must fire and every
    `should_pass` text must not. The benign sample estimates the false-positive rate on traffic that
    should never be touched; its hits are returned so the operator can see why.
    """
    policy = compile_rule(spec, "rule_draft", judge_model=judge_model, taint_labels=taint_labels)
    guard = _scratch_guard(policy, base_dir)
    started = time.perf_counter()
    examples = []
    for expected, texts in (("fire", spec.tests.should_fire), ("pass", spec.tests.should_pass)):
        for text in texts:
            verdict = await _verdict(guard, policy, text)
            ok = verdict["fired"] == (expected == "fire") and not verdict["error"]
            examples.append({"text": text, "expected": expected, "ok": ok, **verdict})

    benign: dict[str, Any] | None = None
    if benign_texts:
        hits = []
        for record_id, text in benign_texts:
            verdict = await _verdict(guard, policy, text)
            if verdict["fired"]:
                hits.append({"record_id": record_id, "text": text[:300], "reasons": verdict["reasons"]})
        benign = {
            "checked": len(benign_texts),
            "fired": len(hits),
            "rate": len(hits) / len(benign_texts),
            "samples": hits[:5],
        }
    return {
        "policy": policy.model_dump(mode="json"),
        "examples": examples,
        "passed": all(e["ok"] for e in examples),
        "benign": benign,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def benign_records(repo_root: Path, stages: list[Stage], *, limit: int) -> list[tuple[str, str]]:
    """Benign eval records (no labels) at these stages, test split, golden first. Empty when the eval
    package or its datasets aren't installed (e.g. a slim production image)."""
    try:
        from boundary_eval.dataset import load_records
    except ImportError:
        return []
    paths = sorted((repo_root / "packages/eval/datasets/golden").glob("*.jsonl")) + sorted(
        (repo_root / "packages/eval/datasets/extended").glob("*.jsonl")
    )
    if not paths:
        return []
    wanted = {s.value for s in stages}
    out = []
    for loaded in load_records(paths).records:
        rec = loaded.record
        if not rec.labels and rec.split.value == "test" and rec.stage.value in wanted:
            out.append((rec.id, loaded.text))
            if len(out) >= limit:
                break
    return out
