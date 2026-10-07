from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from boundary_eval.compare import compare, comparison_markdown, load_gates
from boundary_eval.dataset import Label, LoadedRecord, Split, load_records
from boundary_eval.report import to_markdown
from boundary_eval.runner import run_detectors, to_result, write_json
from boundary_guard import Guard, GuardConfig, load_config

DEFAULT_POLICIES = Path("policies/guard.yaml")
DEFAULT_GATES = Path("packages/eval/gates.yaml")
DEFAULT_SCENARIOS = Path("packages/eval/scenarios")
DEFAULT_CASSETTE = Path("packages/eval/cassettes/e2e.json")
SUITES = {
    "golden": [Path("packages/eval/datasets/golden")],
    "extended": [Path("packages/eval/datasets/extended")],
    "all": [Path("packages/eval/datasets")],
}


def _expand(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out.extend(sorted(p.rglob("*.jsonl")) if p.is_dir() else [p])
    return out


def _policy_label_errors(config: GuardConfig) -> list[str]:
    known = {label.value for label in Label}
    return [
        f"policy {p.id}: detects unknown label {label!r}"
        for p in config.policies
        for label in p.detects
        if label not in known
    ]


def _print_coverage(records: list[LoadedRecord]) -> None:
    cells = Counter((r.record.stage.value, r.record.category.value, r.record.split.value) for r in records)
    rows = sorted({(stage, cat) for stage, cat, _ in cells})
    print(f"\n{'stage':<13} {'category':<20} {'dev':>4} {'test':>5} {'train':>6}")
    for stage, cat in rows:
        dev, test, train = (cells[(stage, cat, s.value)] for s in (Split.DEV, Split.TEST, Split.TRAIN))
        print(f"{stage:<13} {cat:<20} {dev:>4} {test:>5} {train:>6}")
    splits = Counter(r.record.split.value for r in records)
    attacks = sum(1 for r in records if r.record.labels)
    print(
        f"\ntotal {len(records)}: {attacks} attacks, {len(records) - attacks} benign; "
        + ", ".join(f"{k}={v}" for k, v in sorted(splits.items()))
    )


def _print_policy_support(records: list[LoadedRecord], config: GuardConfig) -> None:
    """How many positives/negatives each policy would be scored on, per split."""
    print(f"\n{'policy':<34} {'detects':<22} {'dev +/-':>9} {'test +/-':>9}")
    for policy in config.policies:
        if not policy.detects:
            continue
        counts = Counter()
        for r in records:
            if r.record.stage not in policy.stages or not r.record.scored_by(policy.detects):
                continue
            positive = any(label.value in policy.detects for label in r.record.labels)
            counts[(r.record.split, positive)] += 1
        dev = f"{counts[(Split.DEV, True)]}/{counts[(Split.DEV, False)]}"
        test = f"{counts[(Split.TEST, True)]}/{counts[(Split.TEST, False)]}"
        print(f"{policy.id:<34} {','.join(policy.detects):<22} {dev:>9} {test:>9}")

    covered = {label for p in config.policies for label in p.detects}
    uncovered = sorted({label.value for r in records for label in r.record.labels} - covered)
    if uncovered:
        print(f"\nlabels with no policy yet: {', '.join(uncovered)}")


def cmd_validate(args: argparse.Namespace) -> int:
    files = _expand(args.paths)
    if not files:
        print("no .jsonl files found", file=sys.stderr)
        return 2

    report = load_records(files)
    config = load_config(args.policies) if args.policies and Path(args.policies).is_file() else None
    if config is not None:
        report.errors.extend(_policy_label_errors(config))

    for warning in report.warnings:
        print(f"warning: {warning}")
    for error in report.errors:
        print(f"error: {error}")

    print(f"\n{len(report.records)} valid records in {len(files)} file(s)")
    if report.records:
        _print_coverage(report.records)
        if config is not None:
            _print_policy_support(report.records, config)
    return 0 if report.ok else 1


def cmd_show(args: argparse.Namespace) -> int:
    """Print a record's text as detectors see it (placeholders expanded)."""
    report = load_records(_expand(args.paths))
    for loaded in report.records:
        if loaded.record.id == args.id:
            print(loaded.text)
            return 0
    print(f"record {args.id!r} not found", file=sys.stderr)
    return 1


def cmd_detectors(args: argparse.Namespace) -> int:
    paths = [str(p) for p in SUITES[args.suite]] if not args.paths else args.paths
    suite = args.suite if not args.paths else "custom"
    files = _expand(paths)
    if not files:
        print(f"no .jsonl files for suite {suite!r}", file=sys.stderr)
        return 2
    report = load_records(files)
    if not report.ok:
        for error in report.errors:
            print(f"error: {error}", file=sys.stderr)
        print("dataset is invalid; fix it with `boundary-eval validate` first", file=sys.stderr)
        return 2

    # `train` records exist for fitting our own detector (Phase 8) and are never scored.
    splits = set(args.split or [Split.DEV.value, Split.TEST.value])
    records = [r for r in report.records if r.record.split.value in splits]

    guard = Guard.from_yaml(args.policies)
    run = asyncio.run(
        run_detectors(
            guard, records, repeats=args.repeats, suite=suite, enforce_timeouts=args.enforce_timeouts
        )
    )
    result = to_result(run)

    markdown = to_markdown(result)
    if args.out:
        write_json(result, Path(args.out))
        md_path = Path(args.md) if args.md else Path(args.out).with_suffix(".md")
        md_path.write_text(markdown, encoding="utf-8")
        print(f"wrote {args.out} and {md_path}", file=sys.stderr)
    if not args.quiet:
        print(markdown)
    return 0


def cmd_build_extended(args: argparse.Namespace) -> int:
    from boundary_eval.extended.builders import SOURCES, build

    unknown = sorted(set(args.only or []) - set(SOURCES))
    if unknown:
        print(f"unknown source(s): {', '.join(unknown)} (known: {', '.join(SOURCES)})", file=sys.stderr)
        return 2
    counts = build(args.only)
    for name, n in counts.items():
        print(f"{name:<28} {n:>5} records")
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    from boundary_eval.tune import tune_markdown

    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    print(tune_markdown(result, args.max_fpr, args.policy))
    return 0


def _agent_model() -> str:
    """The LLM_MODEL the agent will use (from the environment / root .env)."""
    from boundary_agent.config import Settings

    return Settings().llm_model


def cmd_e2e(args: argparse.Namespace) -> int:
    import asyncio
    from datetime import UTC, datetime

    from boundary_eval.compare import check_e2e_gate, compare_e2e, load_gates
    from boundary_eval.e2e.cassette import Cassette, install
    from boundary_eval.e2e.report import to_markdown as e2e_markdown
    from boundary_eval.e2e.report import to_result as e2e_result
    from boundary_eval.e2e.runner import CONFIGS, run_matrix
    from boundary_eval.e2e.scenarios import load_dir
    from boundary_guard import Guard

    repo_root = Path.cwd()
    scenarios, resolved = load_dir(Path(args.scenarios), repo_root)
    if args.split:
        scenarios = [sc for sc in scenarios if sc.split.value in args.split]
    if not scenarios:
        print("no scenarios found", file=sys.stderr)
        return 2

    config_names = args.config or list(CONFIGS)
    unknown = [c for c in config_names if c not in CONFIGS]
    if unknown:
        print(f"unknown config(s): {', '.join(unknown)} (known: {', '.join(CONFIGS)})", file=sys.stderr)
        return 2
    configs = [CONFIGS[c] for c in config_names]

    from boundary_agent.config import export_env_file

    export_env_file()  # provider keys from the root .env; LiteLLM reads them from the environment
    policy_path = Path(args.policies)
    settings_overrides: dict[str, str] = {"llm_model": args.model} if args.model else {}
    if args.mode == "replay":
        # CI has no provider key, and the planner refuses to start without one. In replay the cassette
        # answers every call (a miss raises before any request is made), so a placeholder is enough.
        settings_overrides["llm_api_key"] = "cassette-replay"
    cassette = Cassette(Path(args.cassette), mode=args.mode)

    async def go():
        with install(cassette):
            return await run_matrix(
                scenarios,
                resolved,
                configs,
                policy_path=policy_path,
                settings_overrides=settings_overrides,
                enforce_timeouts=args.enforce_timeouts,
            )

    run = asyncio.run(go())
    meta = {
        "suite": Path(args.scenarios).name,
        "scenarios": len(scenarios),
        "mode": args.mode,
        "model": args.model or _agent_model(),
        "timeouts": "enforced" if args.enforce_timeouts else "lifted",
        "config_hash": Guard.from_yaml(policy_path).config_hash,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cassette": {"hits": cassette.hits, "misses": cassette.misses, "recorded": cassette.recorded},
    }
    result = e2e_result(run, meta=meta)
    markdown = e2e_markdown(result, split=(args.split[0] if args.split else "test"))
    if args.out:
        write_json(result, Path(args.out))
        Path(args.out).with_suffix(".md").write_text(markdown, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    if not args.quiet:
        print(markdown)
    if args.mode == "replay" and cassette.misses:
        print(f"error: {cassette.misses} cassette misses in replay mode", file=sys.stderr)
        return 1
    if args.gates and Path(args.gates).is_file():
        gate = load_gates(args.gates).e2e
        baseline = None
        if gate.baseline:
            baseline_path = Path(gate.baseline)
            if baseline_path.is_file():
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
                diff = compare_e2e(baseline, result, gate)
                for label, ids in (("improved", diff.improved), ("not in baseline", diff.not_in_baseline)):
                    if ids:
                        print(f"e2e note: {label}: {', '.join(ids)}", file=sys.stderr)
            else:
                print(f"e2e note: no baseline at {baseline_path}; regression gate skipped", file=sys.stderr)
        failures = check_e2e_gate(result, gate, baseline)
        for failure in failures:
            print(f"e2e gate failure: {failure}", file=sys.stderr)
        if failures:
            return 1
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    current = json.loads(Path(args.current).read_text(encoding="utf-8"))
    gates = load_gates(args.gates)
    result = compare(base, current, gates)
    markdown = comparison_markdown(result)
    if args.md:
        Path(args.md).write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="boundary-eval")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="check dataset files for schema errors and leakage")
    validate.add_argument("paths", nargs="+", help=".jsonl files or directories")
    validate.add_argument("--policies", default=str(DEFAULT_POLICIES), help="policy YAML for coverage")
    validate.set_defaults(func=cmd_validate)

    det = sub.add_parser("detectors", help="run every policy over a dataset and report metrics")
    det.add_argument("paths", nargs="*", help="dataset files/dirs (default: the --suite)")
    det.add_argument("--suite", choices=sorted(SUITES), default="golden")
    det.add_argument("--policies", default=str(DEFAULT_POLICIES))
    det.add_argument(
        "--split",
        action="append",
        choices=[s.value for s in Split],
        help="splits to score (default: dev, test)",
    )
    det.add_argument("--repeats", type=int, default=1, help="timed passes; >1 adds a warm-up pass (latency)")
    det.add_argument(
        "--enforce-timeouts",
        action="store_true",
        help="keep production policy timeouts (default: lifted, so slow machines can't flip verdicts)",
    )
    det.add_argument("--out", help="write the result JSON here (and .md next to it)")
    det.add_argument("--md", help="markdown path (default: --out with .md)")
    det.add_argument("--quiet", action="store_true", help="don't print the markdown report")
    det.set_defaults(func=cmd_detectors)

    ext = sub.add_parser("build-extended", help="(re)build the extended set from its pinned sources")
    ext.add_argument("--only", action="append", help="build just this source (repeatable)")
    ext.set_defaults(func=cmd_build_extended)

    tune = sub.add_parser("tune", help="sweep thresholds on the dev split of a detectors result")
    tune.add_argument("result", help="JSON written by `detectors --out`")
    tune.add_argument("--max-fpr", type=float, default=0.05, help="dev FPR budget (default 0.05)")
    tune.add_argument("--policy", action="append", help="only these policies (repeatable)")
    tune.set_defaults(func=cmd_tune)

    e2e = sub.add_parser("e2e", help="run the end-to-end agent scenarios under each defence config")
    e2e.add_argument("scenarios", nargs="?", default=str(DEFAULT_SCENARIOS))
    e2e.add_argument("--policies", default=str(DEFAULT_POLICIES))
    e2e.add_argument("--config", action="append", help="defence config(s) to run (default: all)")
    e2e.add_argument("--split", action="append", choices=["dev", "test"])
    e2e.add_argument("--mode", choices=["replay", "record", "live"], default="replay")
    e2e.add_argument("--cassette", default=str(DEFAULT_CASSETTE))
    e2e.add_argument("--model", help="override LLM_MODEL for this run")
    e2e.add_argument("--out", help="write the result JSON here (and .md next to it)")
    e2e.add_argument("--gates", help="apply the e2e gate from this gates.yaml (fails on violation)")
    e2e.add_argument("--quiet", action="store_true")
    e2e.add_argument(
        "--enforce-timeouts",
        action="store_true",
        help="keep the policies' production timeouts (default: lifted)",
    )
    e2e.set_defaults(func=cmd_e2e)

    cmp = sub.add_parser("compare", help="compare a run against a baseline and apply CI gates")
    cmp.add_argument("baseline")
    cmp.add_argument("current")
    cmp.add_argument("--gates", default=str(DEFAULT_GATES))
    cmp.add_argument("--md", help="also write the comparison markdown here")
    cmp.set_defaults(func=cmd_compare)

    show = sub.add_parser("show", help="print one record's expanded text")
    show.add_argument("id")
    show.add_argument("paths", nargs="*", default=["packages/eval/datasets"])
    show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
