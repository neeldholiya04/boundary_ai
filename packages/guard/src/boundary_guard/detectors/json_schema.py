from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import json_repair
from jsonschema import Draft202012Validator

from boundary_guard.core.detector import Detector, file_digest, register_detector
from boundary_guard.core.types import CheckContext, Detection

_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n\s*```\s*$", re.DOTALL)
_MAX_REPORTED_ERRORS = 5


class JsonSchemaDetector(Detector):
    """Validates structured output against a JSON Schema, repairing malformed JSON first.

    Runs only when the caller asks for this schema (`ctx.response_schema == schema_id`).
    Triggers when the output is still invalid after repair. Syntax repair (fences, trailing
    commas, quotes, truncation) is local; repairing *schema* violations needs an LLM re-ask,
    which is a later addition.
    """

    type_name = "json_schema"
    transform = True

    def __init__(self, schema_path: Path, schema_id: str | None = None, repair: bool = True) -> None:
        self.schema_path = schema_path
        self.schema_id = schema_id or schema_path.name.removesuffix(".json")
        self.repair = repair
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.validator = Draft202012Validator(schema)

    def applies(self, ctx: CheckContext) -> bool:
        return ctx.response_schema == self.schema_id

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        repaired = False
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            if not self.repair:
                return Detection(triggered=True, score=1.0, reasons=["output is not valid JSON"])
            obj = self._repair(text)
            repaired = True

        errors = sorted(self.validator.iter_errors(obj), key=lambda e: list(e.absolute_path))
        if errors:
            reasons = [self._describe(e) for e in errors[:_MAX_REPORTED_ERRORS]]
            if repaired:
                reasons.insert(0, "output needed JSON repair")
            return Detection(triggered=True, score=1.0, reasons=reasons)

        if repaired:
            return Detection(
                triggered=False,
                score=0.0,
                reasons=["repaired malformed JSON"],
                rewrite=json.dumps(obj, ensure_ascii=False),
            )
        return Detection(triggered=False, score=0.0)

    def _repair(self, text: str) -> Any:
        fenced = _FENCE.match(text)
        candidate = fenced.group("body") if fenced else text
        return json_repair.loads(candidate)

    def _describe(self, error) -> str:
        path = "/".join(str(p) for p in error.absolute_path) or "<root>"
        return f"schema: {path}: {error.message}"

    def fingerprint(self) -> str:
        return f"{file_digest(self.schema_path)}:{self.schema_id}:{self.repair}"


@register_detector("json_schema")
def _factory(params: dict[str, Any], base_dir: Path) -> Detector:
    return JsonSchemaDetector(
        base_dir / params["schema"],
        schema_id=params.get("schema_id"),
        repair=params.get("repair", True),
    )
