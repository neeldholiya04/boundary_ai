from __future__ import annotations

from typing import Any

from boundary_eval.report import ci, ms, pct

_LABELS = {
    "tool_output_injection_ours": "ours (DeBERTa-v3-xsmall)",
    "tool_output_injection_protectai": "ProtectAI deberta-v3-base",
    "tool_output_injection_promptguard": "Prompt Guard 2 86M",
}


def comparison_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    lines = [
        "# Tool-output injection: ours vs off-the-shelf",
        "",
        f"dataset `{meta['dataset_hash']}` ({meta['records']} tool-output records) · {meta['generated_at']}",
        "",
        "Scored on the tool-output injection records only. Test split; 95% Wilson intervals in brackets.",
        "",
        "| Detector | Split | Pos / Neg | Catch rate | FPR | p50 / p99 ms |",
        "|---|---|---|---|---|---|",
    ]
    for pid, policy in result["policies"].items():
        label = _LABELS.get(pid, pid)
        lat = policy["latency_ms"]
        for split in ("test", "dev"):
            stats = policy["splits"].get(split)
            if not stats:
                continue
            lines.append(
                f"| {label} | {split} | {stats['positives']} / {stats['negatives']} "
                f"| {pct(stats['catch_rate'])}{ci(stats['catch_rate_ci'])} "
                f"| {pct(stats['fpr'])}{ci(stats['fpr_ci'])} "
                f"| {ms(lat['p50'])} / {ms(lat['p99'])} |"
            )
    return "\n".join(lines) + "\n"
