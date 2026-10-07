"""Build docs/RESULTS.md from the committed result files, so every published number is traceable to
an artifact in the repo and the command that regenerates it.

Inputs (all committed):
    packages/eval/baselines/golden.json     detector eval, golden set (latency: 20 timed passes)
    packages/eval/baselines/extended.json   detector eval, extended set (public benchmarks)
    packages/eval/baselines/e2e.json        end-to-end agent eval, replayed from the cassette
    packages/eval/baselines/loadtest.json   Locust load test (optional until it has been run)
    packages/detector/MODELCARD.md          our detector vs the off-the-shelf baselines
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from boundary_eval.report import pct

BASELINES = Path("packages/eval/baselines")
MODELCARD = Path("packages/detector/MODELCARD.md")
E2E_ORDER = [
    "no_defense",
    "spotlight_only",
    "filters",
    "filters_spotlight",
    "shadow",
    "filters_taint",
    "enforce",
]
E2E_LABELS = {
    "no_defense": "guard off",
    "spotlight_only": "spotlighting only",
    "filters": "guard as shipped, no taint",
    "filters_spotlight": "guard + spotlighting, no taint",
    "shadow": "every policy in shadow + taint",
    "filters_taint": "**as shipped**: guard + spotlighting + taint",
    "enforce": "every policy enforced + taint",
}
PRODUCTION = "filters_taint"


def _load(name: str) -> dict[str, Any] | None:
    path = BASELINES / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _ci(lo_hi: list[float] | None) -> str:
    if not lo_hi:
        return ""
    lo, hi = lo_hi
    return f" [{lo * 100:.0f}–{hi * 100:.0f}]"


def _rate(value: float | None, ci: list[float] | None) -> str:
    return "–" if value is None else f"{pct(value)}{_ci(ci)}"


def _ms(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value:.1f}" if value < 10 else f"{value:.0f}"


# ---- sections -------------------------------------------------------------------------------------


def headline(e2e: dict[str, Any], loadtest: dict[str, Any] | None) -> str:
    off = e2e["configs"]["no_defense"]["test"]
    prod = e2e["configs"][PRODUCTION]["test"]
    benign_fail = 1 - prod["benign_task_success"]
    parts = [
        f"Attack success fell from **{pct(off['attack_success_rate'])} to "
        f"{pct(prod['attack_success_rate'])}** (guard off → as shipped) at a "
        f"**{pct(benign_fail)} benign-task failure rate**"
    ]
    overhead = _overhead(loadtest, users=1) if loadtest else None
    if overhead is not None:
        ram = (loadtest or {}).get("meta", {}).get("machine", {}).get("memory_gb")
        where = (
            f"on a laptop with {ram:g} GB of RAM that pages the models out; see the caveats"
            if ram
            else "see the caveats"
        )
        parts.append(f"adding **{overhead:.0f} ms at p99** per request (load test, one user, {where})")
    cost_per_1k = prod["cost_per_task"] * 1000
    parts.append(
        f"for **${cost_per_1k:.2f} per 1k requests** in LLM spend (the guard's own detectors run locally: $0)"
    )
    n_attacks, n_benign = prod["attacks"], prod["benign"]
    return (
        ", ".join(parts)
        + f". Test split: {n_attacks} attack and {n_benign} benign end-to-end scenarios, so each "
        f"scenario moves a rate by {_swing(n_attacks, n_benign)} points; the confidence intervals below "
        "are wide on purpose."
    )


def _swing(n_attacks: int, n_benign: int) -> str:
    """How many points one scenario moves a rate, e.g. "14–25" for 7 attacks and 4 benign."""
    small, large = sorted((max(n_attacks, 1), max(n_benign, 1)))
    lo, hi = 100 / large, 100 / small
    return f"{lo:.0f}" if round(lo) == round(hi) else f"{lo:.0f}–{hi:.0f}"


def _overhead(loadtest: dict[str, Any], *, users: int) -> float | None:
    def level(profile: str) -> dict[str, Any] | None:
        for p in loadtest["profiles"]:
            if p["profile"] == profile:
                return next((lv for lv in p["levels"] if lv["users"] == users), None)
        return None

    off, shipped = level("filters_off"), level("async")
    if not off or not shipped:
        return None
    return shipped["p99_ms"] - off["p99_ms"]


def e2e_table(e2e: dict[str, Any]) -> str:
    rows = [
        "| Config | What runs | Attack success | Benign task success | Utility under attack | $/task |",
        "|---|---|---|---|---|---|",
    ]
    for name in E2E_ORDER:
        stats = e2e["configs"].get(name, {}).get("test")
        if not stats:
            continue
        rows.append(
            f"| `{name}` | {E2E_LABELS.get(name, name)} | "
            f"{_rate(stats['attack_success_rate'], stats.get('attack_success_ci'))} | "
            f"{_rate(stats['benign_task_success'], stats.get('benign_task_success_ci'))} | "
            f"{pct(stats['utility_under_attack'])} | {stats['cost_per_task']:.4f} |"
        )
    return "\n".join(rows)


def policy_table(result: dict[str, Any], *, split: str = "test") -> str:
    rows = [
        "| Policy | Stage(s) | Mode | Catch rate (95% CI) | FPR (95% CI) | Pos / Neg | p50 / p99 ms | $/1k |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for pid, p in result["policies"].items():
        s = p["splits"].get(split) or {}
        positives, negatives = s.get("positives", 0), s.get("negatives", 0)
        catch = _rate(s.get("catch_rate"), s.get("catch_rate_ci")) if positives else "–"
        fpr = _rate(s.get("fpr"), s.get("fpr_ci")) if negatives else "–"
        lat = p.get("latency_ms") or {}
        mode_note = " (async)" if p["execution"] == "async" else ""
        rows.append(
            f"| `{pid}` | {', '.join(p['stages'])} | {p['mode']}{mode_note} | "
            f"{catch} | {fpr} | {positives} / {negatives} | {_ms(lat.get('p50'))} / {_ms(lat.get('p99'))} | "
            f"{p.get('cost_usd_per_1k', 0):.2f} |"
        )
    return "\n".join(rows)


def stage_latency_table(result: dict[str, Any]) -> str:
    rows = ["| Stage | Checks timed | p50 ms | p95 ms | p99 ms |", "|---|---|---|---|---|"]
    for stage in ("user_input", "tool_args", "tool_output", "final_output"):
        lat = (result.get("stages", {}).get(stage) or {}).get("latency_ms")
        if lat:
            rows.append(
                f"| {stage} | {lat['n']} | {_ms(lat['p50'])} | {_ms(lat['p95'])} | {_ms(lat['p99'])} |"
            )
    return "\n".join(rows)


def detector_table() -> str:
    text = MODELCARD.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.startswith("| ")]
    start = next(i for i, ln in enumerate(lines) if ln.startswith("| Detector"))
    table = [lines[start]] + [
        ln for ln in lines[start + 1 :] if re.match(r"^\| (\*\*ours|ProtectAI|Prompt)", ln)
    ]
    return "\n".join([table[0], "|---|---|---|---|---|", *table[1:]])


def loadtest_section(loadtest: dict[str, Any] | None) -> str:
    if loadtest is None:
        return "_Not run yet: `uv run boundary-eval loadtest --out packages/eval/baselines/loadtest.json`._"
    from boundary_eval.loadtest import to_markdown

    body = to_markdown(loadtest).split("\n", 2)[2]  # drop the "# Load test" title
    body = body.replace("\n## ", "\n### ")  # nest its sections under this one
    return body.strip() + "\n\n" + _loadtest_reading(loadtest)


def _loadtest_reading(loadtest: dict[str, Any]) -> str:
    """How to read the load test, with the figures taken from the run itself."""
    shipped = next((p for p in loadtest["profiles"] if p["profile"] == "async"), None)
    rows = [r for r in (shipped or {}).get("policy_latency", []) if r["execution"] == "blocking"]
    top = sorted(rows, key=lambda r: r["p50_ms"], reverse=True)[:3]
    if not top:
        return ""
    named = ", ".join(f"`{r['policy']}` on {r['stage']} ({r['p50_ms'] / 1000:.1f} s p50)" for r in top)
    ram = loadtest.get("meta", {}).get("machine", {}).get("memory_gb")
    memory = f"{ram:g} GB of RAM" if ram else "the machine's RAM"
    return (
        "**How to read this.** Almost all of the overhead is three transformer checks on the request path: "
        f"{named}. Every other check costs milliseconds. The same models take tens of milliseconds per check "
        "in the detector eval's warm loop, so on this machine they run an order of magnitude slower under "
        f"real load: the models are paged out between requests ({memory} shared with everything else) "
        "and the background groundedness checks compete for the same CPU. Throughput levels off once the "
        "CPU is saturated, and from then on `async` can't help, because the background checks still need "
        "that CPU; it only pays off while there's spare capacity. Two levers follow: a host with enough RAM "
        "to keep the models resident (Phase 12 re-runs this there), and taking shadow-mode checks off the "
        "request path (their verdict never changes the response; only the run's taint needs it before the "
        "next tool call)."
    )


def _meta_line(result: dict[str, Any] | None, label: str) -> str:
    if not result:
        return f"- {label}: not available"
    m = result["meta"]
    bits = [f"policy v{m['policy_version']}"] if m.get("policy_version") else []
    bits.append(f"config `{m.get('config_hash', '?')}`")
    if m.get("records"):
        bits.append(f"{m['records']} records")
    if m.get("model"):
        bits.append(f"model `{m['model']}`")
    bits.append(f"generated {m.get('generated_at', '?')}")
    return f"- {label}: " + ", ".join(bits)


def build() -> str:
    golden, extended, e2e, loadtest = (
        _load(n) for n in ("golden.json", "extended.json", "e2e.json", "loadtest.json")
    )
    if not (golden and extended and e2e):
        raise SystemExit(
            "missing baselines: need golden.json, extended.json and e2e.json in packages/eval/baselines"
        )
    parts = [
        "# Results",
        "",
        "Generated by `uv run boundary-eval results` from the committed result files; every table names the",
        "command that reproduces it. Don't edit by hand.",
        "",
        "## Headline",
        "",
        headline(e2e, loadtest),
        "",
        "## End to end: does the agent get hijacked?",
        "",
        "The real agent (gpt-4.1-mini) on the scenario suite, replayed from the recorded cassette.",
        "Attack success: the agent did the attacker's bidding. Benign task success: a clean task still",
        "got done.",
        "Utility under attack: the user's real task still got done during an attack. 95% Wilson intervals.",
        "",
        e2e_table(e2e),
        "",
        "Reproduce: `uv run boundary-eval e2e` (no key needed). Per-scenario notes:",
        "[EVAL.md](EVAL.md#end-to-end-results).",
        "",
        "## Per policy: catch rate, false positives, latency, cost",
        "",
        "Extended set (public benchmarks + synthetic PII), test split. A dash means the split has no",
        "records of that kind for the policy. Latency is per check on this machine (see caveats).",
        "",
        policy_table(extended),
        "",
        "Golden set (hand-written, includes the hardest cases), test split:",
        "",
        policy_table(golden),
        "",
        "Reproduce: `uv run boundary-eval detectors --suite extended` and `--suite golden --repeats 20`.",
        "",
        "## Latency per guard stage (all of a stage's blocking policies, run concurrently)",
        "",
        stage_latency_table(golden),
        "",
        "From the golden run (20 timed passes after a warm-up). The guard's detectors are local models, so",
        "the guard itself costs $0 per request; the only spend is the agent's own LLM calls.",
        "",
        "## Our detector vs off-the-shelf (tool-output injection, test split)",
        "",
        detector_table(),
        "",
        "Reproduce: see [the model card](../packages/detector/MODELCARD.md#reproduce).",
        "",
        "## Load test: throughput, and blocking vs async",
        "",
        loadtest_section(loadtest),
        "",
        "Reproduce: `uv run boundary-eval loadtest --out packages/eval/baselines/loadtest.json`.",
        "",
        "## Provenance",
        "",
        _meta_line(golden, "golden"),
        _meta_line(extended, "extended"),
        _meta_line(e2e, "end to end"),
        f"- load test: {loadtest['meta']['generated_at']}" if loadtest else "- load test: not run yet",
        "",
        "## Caveats",
        "",
        f"- **Small end-to-end sample.** {_e2e_counts(e2e)}: each one moves a rate by "
        f"{_swing(*_e2e_count_pair(e2e))}",
        "  points. The per-scenario story in EVAL.md matters more than the percentages.",
        "- **Latency was measured on a developer laptop under memory pressure** (models paged out between",
        "  requests), so tail latencies are pessimistic; Phase 12 re-runs the load test on the deployed",
        "  host.",
        "- **The headline's false-positive figure is task-level**: the share of benign end-to-end tasks that",
        "  didn't complete (here, one run held for approval after a harmless article *about* injection was",
        "  flagged). Detector-level FPRs are in the per-policy tables.",
    ]
    return "\n".join(parts) + "\n"


README_START = "<!-- results:start (generated by `boundary-eval results`; don't edit by hand) -->"
README_END = "<!-- results:end -->"


def readme_block() -> str:
    golden, extended, e2e, loadtest = (
        _load(n) for n in ("golden.json", "extended.json", "e2e.json", "loadtest.json")
    )
    if not (golden and extended and e2e):
        raise SystemExit(
            "missing baselines: need golden.json, extended.json and e2e.json in packages/eval/baselines"
        )
    return "\n".join(
        [
            README_START,
            "",
            headline(e2e, loadtest),
            "",
            e2e_table(e2e),
            "",
            "Our fine-tuned tool-output injection detector against the off-the-shelf ones (test split):",
            "",
            detector_table(),
            "",
            "Per-policy catch rates and false positives with confidence intervals, latency per stage, the",
            "load test and every reproduction command: [docs/RESULTS.md](docs/RESULTS.md).",
            "",
            README_END,
        ]
    )


def update_readme(path: Path = Path("README.md")) -> bool:
    """Replace the marked results block in the README. Returns False when the markers are missing."""
    text = path.read_text(encoding="utf-8")
    start, end = text.find(README_START), text.find(README_END)
    if start < 0 or end < 0:
        return False
    path.write_text(text[:start] + readme_block() + text[end + len(README_END) :], encoding="utf-8")
    return True


def _e2e_count_pair(e2e: dict[str, Any] | None) -> tuple[int, int]:
    stats = ((e2e or {}).get("configs", {}).get("filters_taint") or {}).get("test") or {}
    return int(stats.get("attacks") or 0), int(stats.get("benign") or 0)


def _e2e_counts(e2e: dict[str, Any] | None) -> str:
    attacks, benign = _e2e_count_pair(e2e)
    return f"{attacks} attack / {benign} benign test scenarios"
