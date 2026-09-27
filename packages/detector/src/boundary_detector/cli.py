from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def cmd_data(args: argparse.Namespace) -> int:
    from boundary_detector.data import assemble

    manifest = assemble(
        Path(args.out),
        carrier_limit=args.carriers,
        augment_per_carrier=args.augment,
        seed=args.seed,
    )
    print(json.dumps(manifest, indent=2))
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    from boundary_detector.train import TrainConfig, train

    if not (Path(args.data) / "train.jsonl").exists():
        print(f"no training data in {args.data}; run `boundary-detector data` first", file=sys.stderr)
        return 2
    summary = train(
        Path(args.data),
        Path(args.out),
        TrainConfig(base_model=args.base_model, epochs=args.epochs, batch_size=args.batch_size),
    )
    print(json.dumps(summary, indent=2))
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    from boundary_detector.evaluate import evaluate
    from boundary_detector.report import comparison_markdown

    result = evaluate(Path(args.model), threshold=args.threshold, include_baselines=not args.no_baselines)
    markdown = comparison_markdown(result)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        Path(args.out).with_suffix(".md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from boundary_detector.export import export_onnx

    path = export_onnx(Path(args.model))
    print(f"wrote {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="boundary-detector")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("data", help="assemble the training set (train split + augmentation)")
    d.add_argument("--out", default="packages/detector/data")
    d.add_argument("--carriers", type=int, default=400)
    d.add_argument("--augment", type=int, default=2)
    d.add_argument("--seed", type=int, default=20260926)
    d.set_defaults(func=cmd_data)

    t = sub.add_parser("train", help="fine-tune the detector")
    t.add_argument("--data", default="packages/detector/data")
    t.add_argument("--out", default="packages/detector/model")
    t.add_argument("--base-model", default="microsoft/deberta-v3-xsmall")
    t.add_argument("--epochs", type=float, default=3.0)
    t.add_argument("--batch-size", type=int, default=8)
    t.set_defaults(func=cmd_train)

    e = sub.add_parser("evaluate", help="compare ours vs the off-the-shelf baselines on tool output")
    e.add_argument("model")
    e.add_argument("--threshold", type=float, default=0.5)
    e.add_argument("--no-baselines", action="store_true")
    e.add_argument("--out")
    e.set_defaults(func=cmd_evaluate)

    x = sub.add_parser("export", help="export the model to ONNX (optional, for CPU latency)")
    x.add_argument("model")
    x.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
