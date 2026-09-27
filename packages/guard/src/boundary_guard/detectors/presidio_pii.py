from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from boundary_guard.core.detector import Detector, register_detector
from boundary_guard.core.types import CheckContext, Detection, Span
from boundary_guard.detectors._models import shared

# Presidio entity -> placeholder label used in redactions (<EMAIL_1>, ...).
_LABELS = {
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "CREDIT_CARD": "CARD",
    "US_SSN": "SSN",
    "IBAN_CODE": "IBAN",
    "IP_ADDRESS": "IP",
    "PERSON": "PERSON",
    "LOCATION": "LOCATION",
    "US_BANK_NUMBER": "BANK_ACCOUNT",
    "US_PASSPORT": "PASSPORT",
    "US_DRIVER_LICENSE": "DRIVER_LICENSE",
}


def _load(spacy_model: str) -> Any:
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)
    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": spacy_model}],
        }
    )
    return AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["en"])


class PresidioPIIDetector(Detector):
    """PII detection with Microsoft Presidio (pattern + checksum recognisers, spaCy for context).

    Returns one span per finding, so `action: redact` replaces each with a typed placeholder.
    `allow_list` values (e.g. a project's public role mailbox) are never reported.
    """

    type_name = "presidio"

    def __init__(
        self,
        *,
        entities: list[str],
        score_threshold: float,
        spacy_model: str = "en_core_web_sm",
        allow_list: list[str] | None = None,
    ) -> None:
        self.entities = entities
        self.threshold = score_threshold
        self.spacy_model = spacy_model
        self.allow_list = sorted(set(allow_list or []))
        self.loaded = shared(("presidio", spacy_model), lambda: _load(spacy_model))
        self._analyze("warm-up text for john@example.com")

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        return await asyncio.to_thread(self._detect_sync, text)

    def _analyze(self, text: str) -> list[Any]:
        with self.loaded.lock:
            return self.loaded.obj.analyze(
                text=text,
                entities=self.entities,
                language="en",
                score_threshold=self.threshold,
                allow_list=self.allow_list or None,
            )

    def _detect_sync(self, text: str) -> Detection:
        results = self._analyze(text)
        spans = [Span(r.start, r.end, _LABELS.get(r.entity_type, r.entity_type)) for r in results]
        found = sorted({r.entity_type for r in results})
        return Detection(
            triggered=bool(results),
            score=max((r.score for r in results), default=0.0),
            spans=spans,
            reasons=[f"presidio found {', '.join(found)}"] if found else [],
        )

    def fingerprint(self) -> str:
        import presidio_analyzer

        version = getattr(presidio_analyzer, "__version__", "?")
        return f"presidio {version}:{self.spacy_model}:{','.join(self.entities)}:{','.join(self.allow_list)}"


@register_detector("presidio")
def _factory(params: dict[str, Any], base_dir: Path) -> Detector:
    return PresidioPIIDetector(
        entities=list(params["entities"]),
        score_threshold=float(params.get("score_threshold", 0.5)),
        spacy_model=params.get("spacy_model", "en_core_web_sm"),
        allow_list=list(params.get("allow_list", [])),
    )
