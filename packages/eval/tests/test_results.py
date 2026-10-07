import os
from pathlib import Path

from boundary_eval import results

REPO = Path(__file__).resolve().parents[3]


def test_results_build_from_the_committed_files(monkeypatch):
    monkeypatch.chdir(REPO)
    text = results.build()
    assert text.startswith("# Results")
    for section in ("## Headline", "## End to end", "## Per policy", "## Our detector", "## Load test"):
        assert section in text
    assert "Attack success fell from" in text
    assert "`filters_taint`" in text and "82.8%" in text  # e2e table + our detector row
    assert "Reproduce:" in text


def test_overhead_uses_the_shipped_profile_against_guard_off():
    loadtest = {
        "profiles": [
            {"profile": "filters_off", "levels": [{"users": 1, "p99_ms": 2000}]},
            {"profile": "async", "levels": [{"users": 1, "p99_ms": 2600}]},
        ]
    }
    assert results._overhead(loadtest, users=1) == 600
    assert results._overhead(loadtest, users=4) is None
    assert os.path.basename(str(results.BASELINES)) == "baselines"


def test_readme_block_is_replaced_between_markers(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    readme = tmp_path / "README.md"
    readme.write_text(
        f"intro\n{results.README_START}\nstale\n{results.README_END}\noutro\n", encoding="utf-8"
    )
    assert results.update_readme(readme)
    text = readme.read_text(encoding="utf-8")
    assert "stale" not in text and "Attack success fell from" in text
    assert text.startswith("intro\n") and text.endswith("outro\n")
    no_markers = tmp_path / "plain.md"
    no_markers.write_text("no markers here\n", encoding="utf-8")
    assert not results.update_readme(no_markers)
