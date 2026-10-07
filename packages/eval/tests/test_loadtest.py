from pathlib import Path

import yaml

from boundary_eval import loadtest

REPO = Path(__file__).resolve().parents[3]


def test_loadtest_database_is_separate_from_the_apps():
    pg = "postgresql+asyncpg://u:p@localhost:5433/boundary"
    assert loadtest.loadtest_database_url(pg) == "postgresql+asyncpg://u:p@localhost:5433/boundary_loadtest"
    assert (
        loadtest.loadtest_database_url("sqlite+aiosqlite:///./boundary.db")
        == "sqlite+aiosqlite:///./boundary_loadtest.db"
    )


def test_blocking_profile_makes_every_async_policy_blocking(tmp_path):
    target = loadtest.write_blocking_policy(REPO / "policies" / "guard.yaml", tmp_path / "blocking.yaml")
    original = yaml.safe_load((REPO / "policies" / "guard.yaml").read_text())
    rewritten = yaml.safe_load(target.read_text())
    was_async = {p["id"] for p in original["policies"] if p.get("execution") == "async"}
    assert was_async, "the production policy should have at least one async check"
    assert all(p.get("execution") != "async" for p in rewritten["policies"])
    assert [p["id"] for p in rewritten["policies"]] == [p["id"] for p in original["policies"]]
    # File parameters are absolute, so the copy works outside policies/ (e.g. a read-only image).
    files = [
        v
        for p in rewritten["policies"]
        for k, v in p["detector"].items()
        if k in ("ruleset", "allow", "schema")
    ]
    assert files and all(Path(f).is_absolute() and Path(f).exists() for f in files)
    from boundary_guard import load_config

    assert load_config(target).version == original["version"]  # valid config from the temp dir


def test_sandbox_page_follows_the_container_sandbox_root(monkeypatch):
    monkeypatch.setenv("BOUNDARY_SANDBOX_ROOT", "/data/mcp-sandbox")
    assert loadtest.sandbox_page_path() == Path("/data/mcp-sandbox/loadtest/page.md")


def test_metric_total_counts_app_source_only():
    text = (
        'guard_errors_total{policy="pii",source="app",stage="user_input"} 2.0\n'
        'guard_errors_total{policy="pii",source="playground",stage="user_input"} 5.0\n'
        'guard_errors_created{policy="pii",source="app",stage="user_input"} 1.7e9\n'
    )
    assert loadtest._metric_total(text, "guard_errors_total") == 2.0


def test_report_computes_overhead_against_filters_off():
    def level(p50, p99):
        return {
            "users": 1,
            "requests": 10,
            "failures": 0,
            "rps": 1.0,
            "p50_ms": p50,
            "p95_ms": p99,
            "p99_ms": p99,
            "avg_ms": p50,
            "failure_reasons": {},
            "guard_errors": 0,
        }

    result = {
        "meta": {
            "stub_llm_latency_ms": 800,
            "duration_s": 45,
            "users": [1],
            "database": "postgres",
            "machine": {"platform": "x", "cpu_count": 8, "memory_gb": 16, "python": "3.12"},
        },
        "profiles": [
            {"profile": "filters_off", "policy": "(none)", "levels": [level(2000, 2500)]},
            {"profile": "async", "policy": "p", "levels": [level(2300, 3100)]},
            {"profile": "blocking", "policy": "p", "levels": [level(4000, 6500)]},
        ],
    }
    md = loadtest.to_markdown(result)
    assert "| 1 | +300 / +600 ms | +2000 / +4000 ms | +1700 / +3400 ms |" in md
    result["profiles"][2]["levels"][0].update(p50_ms=2000, p99_ms=2500)  # blocking faster than async
    assert "| 1 | +300 / +600 ms | +0 / +0 ms | -300 / -600 ms |" in loadtest.to_markdown(result)
