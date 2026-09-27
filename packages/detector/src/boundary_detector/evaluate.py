"""Score our detector against the off-the-shelf baselines on the tool-output injection test split.

Builds a throwaway guard config with three shadow policies on tool_output (ours + ProtectAI +
Prompt Guard 86M), runs the existing detector eval on the tool-output records only, and returns the
per-detector table. Reuses the tested `boundary_eval` runner, so the numbers are comparable to the
main eval.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import yaml

from boundary_eval.dataset import load_records
from boundary_eval.runner import run_detectors, to_result
from boundary_guard import Guard

BASELINES = {
    "ours": None,  # filled with the model path
    "protectai": {
        "type": "hf_classifier",
        "model": "protectai/deberta-v3-base-prompt-injection-v2",
        "revision": "90c9989b1a342275dd0d1a95aad283c04e075671",
        "positive_labels": ["INJECTION"],
        "threshold": 0.5,
        "chunking": {"max_tokens": 512, "overlap_tokens": 64, "max_chunks": 16},
    },
    "promptguard": {
        "type": "hf_classifier",
        "model": "meta-llama/Llama-Prompt-Guard-2-86M",
        "revision": "a8ded8e697ce7c355e395a0df51f94adb4a2fd27",
        "positive_labels": ["LABEL_1"],
        "threshold": 0.5,
        "chunking": {"max_tokens": 512, "overlap_tokens": 64, "max_chunks": 16},
    },
}


def _policies(model_path: Path, threshold: float, include_baselines: bool) -> list[dict[str, Any]]:
    ours = {
        "id": "tool_output_injection_ours",
        "stages": ["tool_output"],
        "detects": ["injection"],
        "mode": "shadow",
        "action": "block",
        "timeout_ms": 10000,
        "detector": {
            "type": "hf_classifier",
            "model": str(model_path),
            "positive_labels": ["INJECTION"],
            "threshold": threshold,
            "chunking": {"max_tokens": 512, "overlap_tokens": 64, "max_chunks": 16},
        },
    }
    policies = [ours]
    if include_baselines:
        for name, det in (("protectai", BASELINES["protectai"]), ("promptguard", BASELINES["promptguard"])):
            policies.append(
                {
                    "id": f"tool_output_injection_{name}",
                    "stages": ["tool_output"],
                    "detects": ["injection"],
                    "mode": "shadow",
                    "action": "block",
                    "timeout_ms": 10000,
                    "detector": det,
                }
            )
    return policies


def evaluate(
    model_path: Path,
    *,
    threshold: float = 0.5,
    include_baselines: bool = True,
    datasets: list[Path] | None = None,
) -> dict[str, Any]:
    datasets = datasets or [Path("packages/eval/datasets/golden"), Path("packages/eval/datasets/extended")]
    files = [f for d in datasets for f in sorted(d.glob("*.jsonl"))]
    report = load_records(files)
    records = [r for r in report.records if r.record.stage.value == "tool_output"]

    config = {"version": 1, "policies": _policies(model_path, threshold, include_baselines)}
    with tempfile.TemporaryDirectory() as tmp:
        policy_file = Path(tmp) / "eval.yaml"
        policy_file.write_text(yaml.safe_dump(config), encoding="utf-8")
        guard = Guard.from_yaml(policy_file)
        run = asyncio.run(run_detectors(guard, records, suite="tool-output-injection"))
    return to_result(run)
