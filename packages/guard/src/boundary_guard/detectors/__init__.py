"""Built-in detectors. Importing this package registers them.

Detectors with heavy optional dependencies (ML models) import those lazily inside their
factories, so the core install stays small.
"""

from boundary_guard.detectors import (
    all_of,
    embeddings_topic,
    hf_classifier,
    json_schema,
    keywords,
    llm_judge,
    nli_groundedness,
    pattern,
    presidio_pii,
    regex_rules,
)

__all__ = [
    "all_of",
    "embeddings_topic",
    "hf_classifier",
    "json_schema",
    "keywords",
    "llm_judge",
    "nli_groundedness",
    "pattern",
    "presidio_pii",
    "regex_rules",
]
