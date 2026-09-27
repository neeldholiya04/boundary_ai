"""Assemble the training set for the tool-output injection detector.

Everything comes from the datasets' **train** split plus a clean carrier corpus, so the dev/test
tool-output injection records used for evaluation are never seen in training. Positives are clean
carrier documents with a train-split injection instruction spliced in at a random depth and wrapped
the way real indirect injection hides (HTML comment, "note to AI", code comment, footer, or a plain
paragraph); negatives are the same carriers left clean, plus benign InjecAgent fills. No new attack
prose is written here: the injected instructions are existing vetted train-split records.

Determinism: a single seeded RNG drives every choice, and each output row records the ids of the
carrier and the injection it came from, so the set is reproducible and auditable.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path

from boundary_eval.dataset import load_records

EXTENDED = Path("packages/eval/datasets/extended")
HALUEVAL_REV = "12a856119f03975a94509091e8cada3e6be6ead7"

# Wrappers that hide an instruction inside tool output. `{i}` is the injection text.
WRAPPERS = [
    "{i}",
    "<!-- {i} -->",
    "Note to any AI assistant reading this: {i}",
    "\n\n---\n{i}\n---\n\n",
    "# {i}",
    "[system] {i}",
    '<span style="display:none">{i}</span>',
]


@dataclass(slots=True)
class Example:
    id: str
    text: str
    label: int  # 1 = injection present, 0 = clean
    source: str
    carrier_id: str | None = None
    injection_id: str | None = None


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def _content_hash(text: str) -> str:
    return hashlib.sha256(_norm(text).encode()).hexdigest()


def _train_injections() -> list[tuple[str, str]]:
    """(record id, injection text) for every train-split injection record we can reuse."""
    report = load_records(sorted(EXTENDED.glob("*.jsonl")))
    out: list[tuple[str, str]] = []
    for r in report.records:
        rec = r.record
        if rec.split.value == "train" and "injection" in [label.value for label in rec.labels]:
            out.append((rec.id, r.text))
    return out


def _injecagent_train() -> tuple[list[Example], list[Example]]:
    """InjecAgent train rows already are tool-output-shaped; keep them as-is (pos + benign fills)."""
    report = load_records(sorted(EXTENDED.glob("injecagent.jsonl")))
    pos, neg = [], []
    for r in report.records:
        if r.record.split.value != "train":
            continue
        is_pos = "injection" in [label.value for label in r.record.labels]
        ex = Example(id=r.record.id, text=r.text, label=int(is_pos), source="injecagent-train")
        (pos if is_pos else neg).append(ex)
    return pos, neg


def _carriers(limit: int, seed: int) -> list[tuple[str, str]]:
    """Clean tool-output-shaped documents: HaluEval train news articles (deterministic sample)."""
    from datasets import load_dataset

    ds = load_dataset("pminervini/HaluEval", "summarization", revision=HALUEVAL_REV)
    data = next(iter(ds.values()))
    docs = [(f"halueval-{i}", r["document"]) for i, r in enumerate(data) if 600 <= len(r["document"]) <= 3500]
    docs.sort(key=lambda d: hashlib.sha256(d[1].encode()).hexdigest())
    return docs[:limit]


def _splice(carrier: str, injection: str, rng: random.Random) -> str:
    wrapped = rng.choice(WRAPPERS).format(i=injection.strip())
    paras = carrier.split("\n")
    pos = rng.randint(0, len(paras))
    paras.insert(pos, wrapped)
    return "\n".join(paras)


def build(
    *,
    carrier_limit: int = 400,
    augment_per_carrier: int = 2,
    seed: int = 20260926,
) -> list[Example]:
    rng = random.Random(seed)
    injections = _train_injections()
    carriers = _carriers(carrier_limit, seed)
    ia_pos, ia_neg = _injecagent_train()

    examples: list[Example] = list(ia_pos) + list(ia_neg)
    for carrier_id, carrier in carriers:
        # Clean negative.
        examples.append(
            Example(
                id=f"clean-{carrier_id}", text=carrier, label=0, source="carrier-clean", carrier_id=carrier_id
            )
        )
        # Augmented positives.
        for k in range(augment_per_carrier):
            inj_id, inj_text = injections[rng.randrange(len(injections))]
            examples.append(
                Example(
                    id=f"aug-{carrier_id}-{k}",
                    text=_splice(carrier, inj_text, rng),
                    label=1,
                    source="carrier-augmented",
                    carrier_id=carrier_id,
                    injection_id=inj_id,
                )
            )
    rng.shuffle(examples)
    return examples


def leakage_hashes(
    dataset_dir: Path = EXTENDED, golden_dir: Path = Path("packages/eval/datasets/golden")
) -> set[str]:
    """Content hashes of every dev/test tool-output injection record — must not appear in training."""
    hashes: set[str] = set()
    for directory in (dataset_dir, golden_dir):
        report = load_records(sorted(directory.glob("*.jsonl")))
        for r in report.records:
            if r.record.split.value in ("dev", "test") and r.record.stage.value == "tool_output":
                hashes.add(_content_hash(r.text))
    return hashes


def write_jsonl(examples: list[Example], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ex in examples:
            fh.write(json.dumps(asdict(ex), ensure_ascii=False) + "\n")


def assemble(out_dir: Path, **kwargs) -> dict[str, object]:
    """Build the training set, split off a small internal validation slice, and check for leakage."""
    examples = build(**kwargs)
    forbidden = leakage_hashes()
    leaks = [ex.id for ex in examples if _content_hash(ex.text) in forbidden]
    if leaks:
        raise RuntimeError(f"training set overlaps dev/test tool-output records: {leaks[:5]}")

    # Internal 90/10 train/validation split by hash (for early stopping; not the eval test set).
    def is_val(ex: Example) -> bool:
        return int(hashlib.sha256(ex.id.encode()).hexdigest()[:8], 16) % 10 == 0

    train = [ex for ex in examples if not is_val(ex)]
    val = [ex for ex in examples if is_val(ex)]
    write_jsonl(train, out_dir / "train.jsonl")
    write_jsonl(val, out_dir / "val.jsonl")

    def counts(rows: list[Example]) -> dict[str, int]:
        return {
            "total": len(rows),
            "positive": sum(r.label for r in rows),
            "negative": sum(1 - r.label for r in rows),
        }

    manifest = {
        "seed": kwargs.get("seed", 20260926),
        "sources": {
            "injections": len(_train_injections()),
            "carriers": len(_carriers(kwargs.get("carrier_limit", 400), 0)),
        },
        "train": counts(train),
        "val": counts(val),
        "leakage_checked_against": len(forbidden),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
