from __future__ import annotations

from collections import defaultdict
from typing import Any

from boundary_eval.e2e.runner import E2ERun, ScenarioResult
from boundary_eval.metrics import wilson_interval
from boundary_eval.report import pct


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def summarise_config(results: list[ScenarioResult], split: str | None = None) -> dict[str, Any]:
    rows = [r for r in results if split is None or r.split == split]
    attacks = [r for r in rows if r.kind == "attack"]
    benign = [r for r in rows if r.kind == "benign"]

    asr_n = sum(r.attack_success for r in attacks)
    utility_under_attack_n = sum(r.task_success for r in attacks)
    benign_n = sum(r.task_success for r in benign)
    return {
        "attacks": len(attacks),
        "benign": len(benign),
        "attack_success_rate": _rate(asr_n, len(attacks)),
        "attack_success_ci": wilson_interval(asr_n, len(attacks)),
        "utility_under_attack": _rate(utility_under_attack_n, len(attacks)),
        "benign_task_success": _rate(benign_n, len(benign)),
        "benign_task_success_ci": wilson_interval(benign_n, len(benign)),
        "mean_steps": (sum(r.steps for r in rows) / len(rows)) if rows else None,
        "cost_per_task": (sum(r.cost_usd for r in rows) / len(rows)) if rows else None,
    }


def to_result(run: E2ERun, *, meta: dict[str, Any]) -> dict[str, Any]:
    by_config: dict[str, list[ScenarioResult]] = defaultdict(list)
    for r in run.results:
        by_config[r.config].append(r)
    return {
        "schema_version": 1,
        "meta": meta,
        "configs": {
            name: {
                split: summarise_config(rows, split) for split in ("test", "dev", None) if _has(rows, split)
            }
            for name, rows in by_config.items()
        },
        "results": [
            {
                "scenario_id": r.scenario_id,
                "split": r.split,
                "kind": r.kind,
                "config": r.config,
                "status": r.status,
                "steps": r.steps,
                "cost_usd": r.cost_usd,
                "attack_success": r.attack_success,
                "attack_hits": r.attack_hits,
                "task_success": r.task_success,
                "task_failures": r.task_failures,
            }
            for r in run.results
        ],
    }


def _has(rows: list[ScenarioResult], split: str | None) -> bool:
    return split is None or any(r.split == split for r in rows)


def to_markdown(result: dict[str, Any], split: str = "test") -> str:
    meta = result["meta"]
    lines = [
        "# End-to-end agent eval",
        "",
        f"suite `{meta.get('suite', '?')}` · {meta.get('scenarios', '?')} scenarios · "
        f"mode {meta.get('mode', '?')} · model `{meta.get('model', '?')}` · "
        f"config `{meta.get('config_hash', '?')}` · {meta.get('generated_at', '')}",
        "",
        "ASR = share of attack scenarios where the agent did the attacker's bidding (lower is better). "
        "Utility-under-attack = attack scenarios where the real task still got done. "
        "Benign = task success on clean scenarios. 95% Wilson intervals in brackets.",
        "",
        f"## {split} split",
        "",
        "| Config | ASR | Utility under attack | Benign task success | Mean steps | $/task |",
        "|---|---|---|---|---|---|",
    ]
    order = ["no_defense", "spotlight_only", "filters", "filters_spotlight", "filters_taint", "shadow"]
    configs = result["configs"]
    for name in order + [c for c in configs if c not in order]:
        stats = configs.get(name, {}).get(split)
        if not stats:
            continue
        asr = f"{pct(stats['attack_success_rate'])}{_ci(stats['attack_success_ci'])}"
        benign = f"{pct(stats['benign_task_success'])}{_ci(stats['benign_task_success_ci'])}"
        cost = f"{stats['cost_per_task']:.5f}" if stats["cost_per_task"] is not None else "–"
        steps = f"{stats['mean_steps']:.1f}" if stats["mean_steps"] is not None else "–"
        lines.append(
            f"| `{name}` | {asr} | {pct(stats['utility_under_attack'])} | {benign} | {steps} | {cost} |"
        )
    return "\n".join(lines) + "\n"


def _ci(interval: list[float] | tuple[float, float] | None) -> str:
    if not interval:
        return ""
    lo, hi = interval
    return f" [{lo * 100:.0f}–{hi * 100:.0f}]"
