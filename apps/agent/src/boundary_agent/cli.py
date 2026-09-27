"""`boundary`: the single entrypoint for running the application.

    uv run boundary serve            # the guarded agent API (reads the root .env)
    uv run boundary serve --demo     # no keys needed: mock planner, SQLite, no Exa
    uv run boundary web              # the dashboard (Next.js dev server)
    uv run boundary dev --demo       # API and dashboard together
    uv run boundary status           # what `serve` would run with: model, database, guard policies

Settings come from environment variables and the root `.env` (see .env.example); flags here only
override them for this process. The eval and detector pipelines have their own CLIs
(`boundary-eval`, `boundary-detector`).
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys

from boundary_agent.config import ENV_FILE, REPO_ROOT

WEB_DIR = REPO_ROOT / "apps" / "dashboard"
RELOAD_DIRS = [
    str(REPO_ROOT / d) for d in ("apps/agent/src", "apps/sandbox-mcp/src", "packages/guard/src", "policies")
]

# Enough to run the whole loop locally without an LLM key or a network MCP server.
DEMO_ENV = {
    "LLM_PROVIDER": "mock",
    "ALLOW_DEMO_MOCK_PLANNER": "true",
    "EXA_MCP_ENABLED": "false",
}
DEMO_DATABASE_URL = "sqlite+aiosqlite:///./demo.db"


def _apply_overrides(args: argparse.Namespace) -> None:
    """Translate flags into env vars before the app (and its Settings) is imported."""
    if getattr(args, "demo", False):
        os.environ.update(DEMO_ENV)
        os.environ.setdefault("DATABASE_URL", DEMO_DATABASE_URL)
        os.environ.setdefault("REDIS_URL", "")
        os.environ.setdefault("GUARD_POLICY_PATH", "policies/guard.yaml")
    if getattr(args, "no_guard", False):
        os.environ["GUARD_POLICY_PATH"] = ""
    elif getattr(args, "policy", None):
        os.environ["GUARD_POLICY_PATH"] = args.policy
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _web_command(api_url: str) -> tuple[list[str], dict[str, str]]:
    npm = shutil.which("npm")
    if npm is None:
        sys.exit("npm not found; install Node.js 22+ to run the dashboard.")
    if not (WEB_DIR / "node_modules").is_dir():
        sys.exit("dashboard dependencies missing; run: npm --prefix apps/dashboard install")
    env = {**os.environ, "NEXT_PUBLIC_API_BASE_URL": api_url, "NEXT_TELEMETRY_DISABLED": "1"}
    return [npm, "--prefix", str(WEB_DIR), "run", "dev"], env


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    _apply_overrides(args)
    os.chdir(REPO_ROOT)  # relative DATABASE_URL / GUARD_POLICY_PATH resolve from the repo root
    uvicorn.run(
        "boundary_agent.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=RELOAD_DIRS if args.reload else None,
    )


def cmd_serve(args: argparse.Namespace) -> None:
    _serve(args)


def cmd_web(args: argparse.Namespace) -> None:
    command, env = _web_command(args.api_url)
    raise SystemExit(subprocess.call(command, env=env))


def cmd_dev(args: argparse.Namespace) -> None:
    command, env = _web_command(f"http://{args.host}:{args.port}")
    web = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        _serve(args)
    finally:
        if web.poll() is None:
            os.killpg(web.pid, signal.SIGTERM)
            web.wait(timeout=10)


def _masked(url: str) -> str:
    """The URL with any password replaced by ***, so `status` output is safe to paste."""
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable URL>"


def cmd_status(args: argparse.Namespace) -> None:
    _apply_overrides(args)
    from boundary_agent.config import get_settings
    from boundary_guard import load_config

    settings = get_settings()  # also exports .env for LiteLLM
    print(f"env file      {ENV_FILE if ENV_FILE.is_file() else f'{ENV_FILE} (missing; using defaults)'}")
    print(f"database      {_masked(settings.database_url)}")
    print(f"redis         {_masked(settings.redis_url) if settings.redis_url else 'off (in-process events)'}")
    if settings.llm_provider == "mock":
        print("planner       mock (demo only)")
    else:
        print(f"planner       {settings.llm_model} via LiteLLM")
    print(f"exa mcp       {'on' if settings.exa_mcp_enabled else 'off'}")

    policy_path = settings.resolved_guard_policy_path()
    if policy_path is None:
        print("guard         off (GUARD_POLICY_PATH is empty)")
        return
    config = load_config(policy_path)
    shown = policy_path.relative_to(REPO_ROOT) if policy_path.is_relative_to(REPO_ROOT) else policy_path
    print(f"guard         {shown} (v{config.version}, {len(config.policies)} policies)")
    for policy in config.policies:
        stages = ",".join(s.value for s in policy.stages)
        print(
            f"  {policy.id:<34} {policy.mode.value:<8} {policy.action.value:<9} "
            f"{policy.execution.value:<9} {stages}"
        )


def _add_server_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart on code or policy changes")
    _add_config_flags(parser)


def _add_config_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--demo", action="store_true", help="mock planner, SQLite, no Exa: runs without keys")
    guard = parser.add_mutually_exclusive_group()
    guard.add_argument("--policy", help="guard policy file (default: GUARD_POLICY_PATH)")
    guard.add_argument("--no-guard", action="store_true", help="run the agent without the guard")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="boundary", description="Run the boundary-ai guarded agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the agent API")
    _add_server_flags(serve)
    serve.set_defaults(func=cmd_serve)

    dev = sub.add_parser("dev", help="run the agent API and the dashboard together")
    _add_server_flags(dev)
    dev.set_defaults(func=cmd_dev)

    web = sub.add_parser("web", help="run the dashboard against a running API")
    web.add_argument("--api-url", default="http://127.0.0.1:8000")
    web.set_defaults(func=cmd_web)

    status = sub.add_parser("status", help="show the configuration `serve` would use")
    _add_config_flags(status)
    status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
