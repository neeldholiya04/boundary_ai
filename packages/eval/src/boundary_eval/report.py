from __future__ import annotations

from typing import Any


def pct(value: float | None, digits: int = 1) -> str:
    return "–" if value is None else f"{value * 100:.{digits}f}%"


def ci(interval: list[float] | tuple[float, float] | None) -> str:
    if not interval:
        return ""
    lo, hi = interval
    return f" [{lo * 100:.0f}–{hi * 100:.0f}]"


def ms(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value:.2f}" if value < 10 else f"{value:.0f}"


def _policy_row(pid: str, policy: dict[str, Any], split: str) -> str | None:
    stats = policy["splits"].get(split)
    if stats is None:
        return None
    lat = policy["latency_ms"]
    return (
        f"| `{pid}` | {', '.join(policy['detects'])} | {policy['mode']} "
        f"| {stats['positives']} / {stats['negatives']} "
        f"| {pct(stats['catch_rate'])}{ci(stats['catch_rate_ci'])} "
        f"| {pct(stats['fpr'])}{ci(stats['fpr_ci'])} "
        f"| {pct(stats['precision'], 0)} | {ms(lat['p50'])} / {ms(lat['p99'])} "
        f"| {policy['cost_usd_per_1k']:.4f} | {policy['errors']} |"
    )


def to_markdown(result: dict[str, Any], *, splits: tuple[str, ...] = ("test", "dev")) -> str:
    meta = result["meta"]
    policies: dict[str, Any] = result["policies"]
    dirty = " (dirty)" if meta["git"].get("dirty") else ""
    lines = [
        f"# Detector eval: {meta['suite']}",
        "",
        f"config `{meta['config_hash']}` (policy v{meta['policy_version']}) · "
        f"dataset `{meta['dataset_hash']}` ({meta['records']} records) · "
        f"git `{meta['git']['sha']}`{dirty} · {meta['generated_at']} · repeats={meta['repeats']} "
        f"· timeouts {meta.get('timeouts', 'enforced')}",
        "",
        "Catch rate = share of positives the policy fired on; FPR = share of negatives it fired on. "
        "Brackets are 95% Wilson intervals. Scored on `would_action`, so shadow policies count. "
        "Latency is per policy per check (ms).",
    ]

    for split in splits:
        rows = [r for pid, p in policies.items() if (r := _policy_row(pid, p, split))]
        if not rows:
            continue
        lines += [
            "",
            f"## {split} split",
            "",
            "| Policy | Detects | Mode | Pos / Neg | Catch rate | FPR | Precision "
            "| p50 / p99 ms | $/1k | Errors |",
            "|---|---|---|---|---|---|---|---|---|---|",
            *rows,
        ]

    primary = splits[0]
    drill = []
    for pid, policy in policies.items():
        stats = policy["splits"].get(primary)
        if not stats or not (stats["false_negatives"] or stats["false_positives"]):
            continue
        if stats["false_negatives"]:
            drill.append(f"- `{pid}` missed: {', '.join(f'`{i}`' for i in stats['false_negatives'])}")
        if stats["false_positives"]:
            drill.append(f"- `{pid}` false alarms: {', '.join(f'`{i}`' for i in stats['false_positives'])}")
    if drill:
        lines += ["", f"## Misses and false alarms ({primary})", "", *drill]

    source_rows = []
    for pid, policy in policies.items():
        stats = policy["splits"].get(primary)
        if not stats or len(stats.get("by_source", {})) < 2:
            continue
        for source, c in stats["by_source"].items():
            source_rows.append(
                f"| `{pid}` | {source} | {c['tp']} / {c['tp'] + c['fn']} | {pct(c['catch_rate'])} "
                f"| {c['fp']} / {c['fp'] + c['tn']} | {pct(c['fpr'])} |"
            )
    if source_rows:
        lines += [
            "",
            f"## By source ({primary})",
            "",
            "| Policy | Source | Caught | Catch rate | False alarms | FPR |",
            "|---|---|---|---|---|---|",
            *source_rows,
        ]

    tech_rows = []
    for pid, policy in policies.items():
        stats = policy["splits"].get("all")
        if not stats:
            continue
        for category, cell in stats["by_category"].items():
            tech_rows.append(f"| `{pid}` | {category} | {cell['fired']} / {cell['n']} |")
    if tech_rows:
        lines += [
            "",
            "## Fired by category (all splits)",
            "",
            "For attack categories this is the catch count; for `benign` it is the false-alarm count.",
            "",
            "| Policy | Category | Fired / records |",
            "|---|---|---|",
            *tech_rows,
        ]

    stage_rows = [
        f"| {stage} | {s['latency_ms']['n']} | {ms(s['latency_ms']['p50'])} "
        f"| {ms(s['latency_ms']['p95'])} | {ms(s['latency_ms']['p99'])} |"
        for stage, s in result["stages"].items()
    ]
    if stage_rows:
        lines += [
            "",
            "## Whole-check latency by stage (ms, blocking policies, run concurrently)",
            "",
            "| Stage | Samples | p50 | p95 | p99 |",
            "|---|---|---|---|---|",
            *stage_rows,
        ]
    return "\n".join(lines) + "\n"
