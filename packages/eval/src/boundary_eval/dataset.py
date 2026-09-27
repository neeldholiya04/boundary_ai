from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from boundary_eval.fakes import UnknownPlaceholderError, expand, placeholders_in
from boundary_guard import CheckContext, Stage


class Split(StrEnum):
    TRAIN = "train"
    DEV = "dev"
    TEST = "test"


class Label(StrEnum):
    """Ground-truth concepts. Policies declare which ones they detect (`detects:` in the YAML)."""

    INJECTION = "injection"
    JAILBREAK = "jailbreak"
    PII = "pii"
    SECRET = "secret"
    TOXICITY = "toxicity"
    OFF_TOPIC = "off_topic"
    HALLUCINATION = "hallucination"
    SCHEMA_INVALID = "schema_invalid"


class Category(StrEnum):
    """The technique family a record exercises; drives the coverage table."""

    DIRECT_INJECTION = "direct_injection"
    JAILBREAK = "jailbreak"
    INDIRECT_INJECTION = "indirect_injection"
    PII = "pii"
    SECRET = "secret"
    TOXICITY = "toxicity"
    OFF_TOPIC = "off_topic"
    HALLUCINATION = "hallucination"
    SCHEMA = "schema"
    BENIGN = "benign"


# Every non-benign category must carry its matching label.
CATEGORY_LABEL: dict[Category, Label] = {
    Category.DIRECT_INJECTION: Label.INJECTION,
    Category.INDIRECT_INJECTION: Label.INJECTION,
    Category.JAILBREAK: Label.JAILBREAK,
    Category.PII: Label.PII,
    Category.SECRET: Label.SECRET,
    Category.TOXICITY: Label.TOXICITY,
    Category.OFF_TOPIC: Label.OFF_TOPIC,
    Category.HALLUCINATION: Label.HALLUCINATION,
    Category.SCHEMA: Label.SCHEMA_INVALID,
}

CONTEXT_FIELDS = {f.name for f in dataclasses.fields(CheckContext)}


class Record(BaseModel):
    """One detector-level eval example.

    `labels` is closed-world: it lists every concept present in the text, and anything not
    listed is absent, except concepts listed in `unlabeled`, which were never annotated.
    A policy is scored positive on a record if any of its `detects` labels
    is present, negative otherwise (only at the stages the policy runs on). Benign records
    have no labels.

    Exactly one of `text` / `fixture` is set; `fixture` is relative to the dataset file.
    Both may contain `{{fake:<kind>}}` placeholders (see fakes.py).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    split: Split
    source: str = Field(min_length=1)
    stage: Stage
    category: Category
    text: str | None = None
    fixture: str | None = None
    labels: list[Label] = Field(default_factory=list)
    # Concepts nobody annotated for this record (e.g. `off_topic` on generic chatbot prompts from a
    # public dataset). Policies detecting any of these are not scored on the record at all.
    unlabeled: list[Label] = Field(default_factory=list)
    # CheckContext fields for this record, e.g. {"tool_name": "fetch_url"} or {"references": [...]}.
    context: dict[str, object] = Field(default_factory=dict)
    notes: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Record:
        if (self.text is None) == (self.fixture is None):
            raise ValueError("exactly one of `text` or `fixture` must be set")
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("duplicate labels")
        if set(self.labels) & set(self.unlabeled):
            raise ValueError("a label cannot be both present and unlabeled")
        if self.category is Category.BENIGN:
            if self.labels:
                raise ValueError("benign records must have no labels")
        elif CATEGORY_LABEL[self.category] not in self.labels:
            raise ValueError(
                f"category {self.category.value} requires label {CATEGORY_LABEL[self.category].value}"
            )
        unknown = set(self.context) - CONTEXT_FIELDS
        if unknown:
            raise ValueError(f"unknown context fields: {', '.join(sorted(unknown))}")
        return self

    def scored_by(self, detects: set[str] | list[str]) -> bool:
        """False when the record was never annotated for what this policy detects."""
        return not {label.value for label in self.unlabeled}.intersection(detects)

    def check_context(self) -> CheckContext:
        return CheckContext(**self.context)


@dataclass(slots=True)
class LoadedRecord:
    record: Record
    # Text as stored (placeholders intact); used for hashing so leakage checks are stable.
    raw_text: str
    # Text the detectors see (placeholders expanded).
    text: str
    path: Path
    line: int

    @property
    def content_hash(self) -> str:
        normalized = " ".join(self.raw_text.split()).lower()
        return hashlib.sha256(normalized.encode()).hexdigest()


@dataclass(slots=True)
class ValidationReport:
    records: list[LoadedRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_records(paths: list[Path]) -> ValidationReport:
    """Parse and cross-check JSONL datasets.

    Errors: malformed records, duplicate ids, missing fixtures, unknown placeholders, and the
    same content appearing in more than one split (train/test leakage).
    """
    report = ValidationReport()
    seen_ids: dict[str, str] = {}
    by_hash: dict[str, LoadedRecord] = {}

    for path in paths:
        # JSONL lines end at "\n" only; str.splitlines() would also split on U+2028/U+2029,
        # which are legal inside JSON strings.
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
            if not raw.strip():
                continue
            where = f"{path}:{lineno}"
            try:
                record = Record.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValidationError) as exc:
                report.errors.append(f"{where}: {_first_line(exc)}")
                continue

            if record.id in seen_ids:
                report.errors.append(f"{where}: duplicate id {record.id!r} (first at {seen_ids[record.id]})")
                continue
            seen_ids[record.id] = where

            if record.fixture is not None:
                fixture_path = (path.parent / record.fixture).resolve()
                if not fixture_path.is_file():
                    report.errors.append(f"{where}: fixture not found: {record.fixture}")
                    continue
                raw_text = fixture_path.read_text(encoding="utf-8")
            else:
                raw_text = record.text or ""

            try:
                text = expand(raw_text, seed=record.id)
            except UnknownPlaceholderError as exc:
                report.errors.append(f"{where}: {exc}")
                continue

            loaded = LoadedRecord(record, raw_text, text, path, lineno)
            previous = by_hash.get(loaded.content_hash)
            if previous is not None and previous.record.split != record.split:
                report.errors.append(
                    f"{where}: same content as {previous.record.id!r} but in split "
                    f"{record.split.value} vs {previous.record.split.value} (leakage)"
                )
            by_hash.setdefault(loaded.content_hash, loaded)

            if record.category is Category.SECRET and not placeholders_in(raw_text):
                report.warnings.append(f"{where}: secret record without {{{{fake:...}}}} placeholders")

            report.records.append(loaded)
    return report


def _first_line(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        err = exc.errors()[0]
        loc = ".".join(str(p) for p in err["loc"]) or "<record>"
        return f"{loc}: {err['msg']}"
    return str(exc).splitlines()[0]
