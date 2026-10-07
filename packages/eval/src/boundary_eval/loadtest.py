"""Load test: throughput and latency of the agent with the guard off, blocking-only, and as shipped.

For each profile this starts the agent (stub planner: a fixed simulated LLM latency, so the numbers
isolate the guard and the agent loop), warms it up, runs Locust at each concurrency level, and
records requests/s, p50/p95/p99 and failures (a benign request the agent didn't complete, e.g. the
guard blocked it because a check timed out). Guard timeouts are the production ones here: this is
where timeout behaviour gets measured.

Profiles:
- filters_off: no guard at all (the baseline).
- async:       the production policy (policies/guard.yaml); slow checks such as NLI groundedness run
               in the background after the answer.
- blocking:    the same policies with every async one made blocking, so the difference to `async` is
               the cost of keeping slow checks off the request path.

The load test uses its own database (`<name>_loadtest`, created if missing) so it never clutters the
app's conversations, and tracing is turned off for it.
"""

from __future__ import annotations

import csv
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

PROFILES = ("filters_off", "async", "blocking")
LOCUSTFILE = Path("infra/loadtest/locustfile.py")
PAGE = Path("infra/loadtest/page.md")
SANDBOX_PAGE = Path("apps/sandbox-mcp/sandbox/loadtest/page.md")
BLOCKING_POLICY = Path("policies/.loadtest-blocking.yaml")  # gitignored; written and removed per run


@dataclass(slots=True)
class LevelResult:
    users: int
    requests: int
    failures: int
    rps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    avg_ms: float
    failure_reasons: dict[str, int] = field(default_factory=dict)
    guard_errors: int = 0


@dataclass(slots=True)
class ProfileResult:
    profile: str
    policy: str
    levels: list[LevelResult] = field(default_factory=list)
    # Where the time goes: per-policy check latency over this profile's whole run (Postgres only).
    policy_latency: list[dict[str, Any]] = field(default_factory=list)


# ---- helpers ---------------------------------------------------------------------------------------


def loadtest_database_url(url: str) -> str:
    """The app's database URL with `_loadtest` appended to the database name."""
    if url.startswith("sqlite"):
        return re.sub(r"(\.db)?$", "_loadtest.db", url, count=1)
    base, _, query = url.partition("?")
    head, _, name = base.rpartition("/")
    return f"{head}/{name}_loadtest" + (f"?{query}" if query else "")


async def _ensure_postgres_database(url: str) -> None:
    import asyncpg

    head, _, name = url.split("?")[0].rpartition("/")
    admin = head.replace("postgresql+asyncpg://", "postgresql://") + "/postgres"
    conn = await asyncpg.connect(admin)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _policy_latency(url: str, since: float) -> list[dict[str, Any]]:
    """Per (stage, policy) latency of the guard decisions recorded since `since` (epoch seconds)."""
    import asyncpg

    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        rows = await conn.fetch(
            """
            SELECT stage, policy_id, execution, count(*) AS n,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms) AS p90,
                   percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99,
                   count(*) FILTER (WHERE error IS NOT NULL) AS errors
            FROM guard_decisions
            WHERE created_at >= to_timestamp($1)
            GROUP BY stage, policy_id, execution
            ORDER BY stage, p50 DESC
            """,
            since,
        )
    finally:
        await conn.close()
    return [
        {
            "stage": r["stage"],
            "policy": r["policy_id"],
            "execution": r["execution"],
            "n": r["n"],
            "p50_ms": round(r["p50"] or 0, 1),
            "p90_ms": round(r["p90"] or 0, 1),
            "p99_ms": round(r["p99"] or 0, 1),
            "errors": r["errors"],
        }
        for r in rows
    ]


def write_blocking_policy(source: Path, target: Path = BLOCKING_POLICY) -> Path:
    """A copy of the policy file with every async policy made blocking (kept next to the original so
    its relative ruleset/schema/topic paths still resolve)."""
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    for policy in raw.get("policies", []):
        if policy.get("execution") == "async":
            policy["execution"] = "blocking"
    if raw.get("defaults", {}).get("execution") == "async":
        raw["defaults"]["execution"] = "blocking"
    target.write_text(
        "# Generated by `boundary-eval loadtest` (the `blocking` profile); safe to delete.\n"
        + yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    return target


def _metric_total(text: str, name: str) -> float:
    total = 0.0
    for line in text.splitlines():
        if line.startswith(name) and not line.startswith(f"{name}_created"):
            parts = line.rsplit(" ", 1)
            if len(parts) == 2 and ('source="app"' in line or "{" not in line):
                total += float(parts[1])
    return total


def _read_locust_csv(prefix: Path) -> tuple[dict[str, str], dict[str, int]]:
    with open(f"{prefix}_stats.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    aggregated = next(r for r in rows if r.get("Name") == "Aggregated")
    reasons: dict[str, int] = {}
    failures = Path(f"{prefix}_failures.csv")
    if failures.is_file():
        with open(failures, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                error = re.sub(r"^CatchResponseError\('(.*)'\)$", r"\1", row.get("Error", ""))
                reasons[error] = reasons.get(error, 0) + int(row.get("Occurrences", 0))
    return aggregated, reasons


def _ms(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---- the run ---------------------------------------------------------------------------------------


class AgentServer:
    def __init__(self, port: int, env: dict[str, str], log: Path) -> None:
        self.port = port
        self.env = env
        self.log = log
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> AgentServer:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        out = open(self.log, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "boundary_agent.cli", "serve", "--port", str(self.port)],
            env=self.env,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + 300
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"agent exited during startup; see {self.log}")
            try:
                if httpx.get(f"{self.url}/health", timeout=2).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(2)
        raise RuntimeError(f"agent did not start within 300s; see {self.log}")

    def __exit__(self, *_: Any) -> None:
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)

    def metrics(self) -> str:
        return httpx.get(f"{self.url}/metrics", timeout=10).text


def run_profile(
    profile: str,
    *,
    users: list[int],
    duration_s: int,
    latency_ms: int,
    port: int,
    database_url: str,
    policy: Path,
    workdir: Path,
    warmup: int = 3,
    log: Any = print,
) -> ProfileResult:
    if profile == "filters_off":
        policy_arg = ""
    elif profile == "async":
        policy_arg = str(policy)
    elif profile == "blocking":
        policy_arg = str(write_blocking_policy(policy))
    else:
        raise ValueError(f"unknown profile: {profile}")

    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "LLM_PROVIDER": "stub",
        "ALLOW_DEMO_MOCK_PLANNER": "true",
        "STUB_LLM_LATENCY_MS": str(latency_ms),
        "STUB_PAGE_PATH": "loadtest/page.md",
        "GUARD_POLICY_PATH": policy_arg,
        "EXA_MCP_ENABLED": "false",
        "REMOTE_MCP_URL": "",
        "LANGFUSE_PUBLIC_KEY": "",
        "LANGFUSE_SECRET_KEY": "",
        "TOKENIZERS_PARALLELISM": "false",
    }
    result = ProfileResult(profile=profile, policy=policy_arg or "(none)")
    measured_from = 0.0
    with AgentServer(port, env, workdir / f"{profile}-server.log") as server:
        for _ in range(warmup):  # first requests load models and open pools; not measured
            httpx.post(
                f"{server.url}/api/chat", json={"message": "Summarise the vectorlite page."}, timeout=180
            )
        measured_from = time.time()
        for n in users:
            before = _metric_total(server.metrics(), "guard_errors_total")
            prefix = workdir / f"{profile}-u{n}"
            log(f"  {profile}: {n} user(s) for {duration_s}s")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "locust",
                    "-f",
                    str(LOCUSTFILE),
                    "--headless",
                    "-u",
                    str(n),
                    "-r",
                    str(n),
                    "-t",
                    f"{duration_s}s",
                    "--host",
                    server.url,
                    "--csv",
                    str(prefix),
                    "--only-summary",
                    "--stop-timeout",
                    "60",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            row, reasons = _read_locust_csv(prefix)
            after = _metric_total(server.metrics(), "guard_errors_total")
            result.levels.append(
                LevelResult(
                    users=n,
                    requests=int(row["Request Count"]),
                    failures=int(row["Failure Count"]),
                    rps=round(_ms(row["Requests/s"]), 3),
                    p50_ms=_ms(row["50%"]),
                    p95_ms=_ms(row["95%"]),
                    p99_ms=_ms(row["99%"]),
                    avg_ms=round(_ms(row["Average Response Time"]), 1),
                    failure_reasons=reasons,
                    guard_errors=int(after - before),
                )
            )
    if profile == "blocking":
        BLOCKING_POLICY.unlink(missing_ok=True)
    if policy_arg and database_url.startswith("postgresql"):
        import asyncio

        result.policy_latency = asyncio.run(_policy_latency(database_url, measured_from))
    return result


def run(
    *,
    profiles: list[str],
    users: list[int],
    duration_s: int,
    latency_ms: int,
    port: int,
    database_url: str,
    policy: Path,
    workdir: Path,
    log: Any = print,
) -> dict[str, Any]:
    import asyncio

    SANDBOX_PAGE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PAGE, SANDBOX_PAGE)
    lt_url = loadtest_database_url(database_url)
    if lt_url.startswith("postgresql"):
        asyncio.run(_ensure_postgres_database(lt_url))
    workdir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    results = []
    for profile in profiles:
        log(f"profile {profile}")
        results.append(
            run_profile(
                profile,
                users=users,
                duration_s=duration_s,
                latency_ms=latency_ms,
                port=port,
                database_url=lt_url,
                policy=policy,
                workdir=workdir,
                log=log,
            )
        )
    return {
        "schema_version": 1,
        "meta": {
            "stub_llm_latency_ms": latency_ms,
            "duration_s": duration_s,
            "users": users,
            "database": "postgres" if lt_url.startswith("postgresql") else "sqlite",
            "machine": {
                "platform": platform.platform(),
                "cpu_count": os.cpu_count(),
                "memory_gb": _memory_gb(),
                "python": platform.python_version(),
            },
            "elapsed_s": round(time.time() - started),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "profiles": [asdict(r) for r in results],
    }


def _memory_gb() -> float | None:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True)
            return round(int(out.stdout) / 1e9, 1)
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9, 1)
    except Exception:
        return None


# ---- report ----------------------------------------------------------------------------------------


def _level(result: dict[str, Any], profile: str, users: int) -> dict[str, Any] | None:
    for p in result["profiles"]:
        if p["profile"] == profile:
            return next((lv for lv in p["levels"] if lv["users"] == users), None)
    return None


def to_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    m = meta["machine"]
    lines = [
        "# Load test",
        "",
        f"Stub planner with {meta['stub_llm_latency_ms']} ms simulated LLM latency per call (two calls per "
        f"request), {meta['duration_s']} s per level, closed loop (each user sends its next request when the "
        f"answer arrives), {meta['database']} database. Machine: {m['platform']}, {m['cpu_count']} CPUs, "
        f"{m['memory_gb']} GB RAM.",
        "",
        "| Profile | Users | Requests | Req/s | p50 ms | p95 ms | p99 ms | Failed | Guard errors |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for p in result["profiles"]:
        for lv in p["levels"]:
            failed = f"{lv['failures']}" + (
                f" ({lv['failures'] / lv['requests']:.0%})" if lv["requests"] else ""
            )
            lines.append(
                f"| `{p['profile']}` | {lv['users']} | {lv['requests']} | {lv['rps']:.2f} | "
                f"{lv['p50_ms']:.0f} | {lv['p95_ms']:.0f} | {lv['p99_ms']:.0f} | {failed} | "
                f"{lv['guard_errors']} |"
            )
    users = meta["users"]
    lines += [
        "",
        "## Guard overhead per request (vs `filters_off`, same concurrency)",
        "",
        "| Users | `async` p50 / p99 | `blocking` p50 / p99 | blocking − async p50 / p99 |",
        "|---|---|---|---|",
    ]
    for n in users:
        off, asy, blk = (_level(result, name, n) for name in ("filters_off", "async", "blocking"))

        def diff(a: dict[str, Any] | None, b: dict[str, Any] | None) -> str:
            if not a or not b:
                return "–"
            return f"{a['p50_ms'] - b['p50_ms']:+.0f} / {a['p99_ms'] - b['p99_ms']:+.0f} ms"

        lines.append(f"| {n} | {diff(asy, off)} | {diff(blk, off)} | {diff(blk, asy)} |")
    for p in result["profiles"]:
        rows = p.get("policy_latency") or []
        if not rows:
            continue
        lines += [
            "",
            f"## Where the time goes: `{p['profile']}` (per check, all levels)",
            "",
            "| Stage | Policy | Execution | Checks | p50 ms | p90 ms | p99 ms | Errors |",
            "|---|---|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {r['stage']} | `{r['policy']}` | {r['execution']} | {r['n']} | {r['p50_ms']:.0f} | "
            f"{r['p90_ms']:.0f} | {r['p99_ms']:.0f} | {r['errors']} |"
            for r in rows
        ]
    reasons: dict[str, int] = {}
    for p in result["profiles"]:
        for lv in p["levels"]:
            for reason, count in lv["failure_reasons"].items():
                key = f"`{p['profile']}`: {reason}"
                reasons[key] = reasons.get(key, 0) + count
    if reasons:
        lines += ["", "Failures by reason: " + "; ".join(f"{k} ×{v}" for k, v in sorted(reasons.items()))]
    return "\n".join(lines) + "\n"
