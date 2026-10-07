from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/agent/src/boundary_agent/config.py -> repository root (where policies/ lives).
REPO_ROOT = Path(__file__).resolve().parents[4]
# The one settings file for the whole repo (template: .env.example at the root).
ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    app_name: str = "boundary-ai"
    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./boundary.db"
    redis_url: str | None = None
    frontend_origin: str = "http://localhost:3000"
    # "litellm" (default; "openai" is accepted as an alias), "mock" (demo) or "stub" (load tests); the
    # last two need ALLOW_DEMO_MOCK_PLANNER=true.
    llm_provider: str = "litellm"
    allow_demo_mock_planner: bool = False
    # LiteLLM model string: the prefix picks the provider, e.g. openai/gpt-4.1-mini,
    # anthropic/claude-haiku-4-5-20251001, gemini/<model>, ollama/<model>.
    llm_model: str = "openai/gpt-4.1-mini"
    # Optional overrides; normally the provider's own env var (OPENAI_API_KEY, ...) is used.
    llm_api_key: str | None = None
    llm_api_base: str | None = None
    llm_temperature: float = 0.0
    # Load-test stub planner: simulated LLM latency per planner call, and the sandbox page it reads.
    stub_llm_latency_ms: int = 800
    stub_page_path: str = "loadtest/page.md"
    llm_timeout_seconds: float = 30.0
    llm_num_retries: int = 2
    # Guard policy file (relative paths resolve from the repo root). Unset disables the guard.
    guard_policy_path: str | None = None
    # Blocked tool output: "continue" hands the planner a withheld stub and carries on;
    # "halt" ends the run.
    guard_tool_output_on_block: Literal["continue", "halt"] = "continue"
    # Wrap tool output in the planner prompt with per-request markers and tell the model it is
    # untrusted data (spotlighting). A prompt-level defence, measured separately in the e2e eval.
    guard_spotlight: bool = True
    # Dataset labels whose detection in tool output taints the run (enforced or shadow policies).
    guard_taint_labels: list[str] = ["injection"]
    # On startup, add `guard_signal` rules (tainted run -> approval for write_file / delete_file)
    # when no guard_signal rule exists yet.
    seed_guard_signal_policies: bool = True
    max_tool_steps: int = 6
    exa_mcp_enabled: bool = True
    exa_mcp_url: str = "https://mcp.exa.ai/mcp"
    exa_api_key: str | None = None
    remote_mcp_url: str | None = None
    remote_mcp_transport: str = "sse"
    remote_mcp_name: str = "context7"
    approval_ttl_seconds: int = 600
    approval_sweeper_interval_seconds: int = 5
    # Tracing (Langfuse). Off unless both keys are set. Traces only ever hold guarded (redacted) text.
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    # Hard cap on real LLM spend per UTC day across the app (0 = no cap). Cassette replays don't count.
    llm_daily_budget_usd: float = 0.0
    # Public playground (scan + attack mode). Abuse controls: input size, per-client rate limits,
    # the daily budget above, fixture-only tools. Live runs = pasted pages through the real model.
    playground_enabled: bool = True
    playground_live_runs: bool = True
    playground_max_input_chars: int = 8000
    playground_scans_per_minute: int = 30
    playground_attacks_per_hour: int = 10

    @field_validator(
        "llm_api_key",
        "llm_api_base",
        "guard_policy_path",
        "redis_url",
        "langfuse_public_key",
        "langfuse_secret_key",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: str | None) -> str | None:
        return value or None

    def resolved_guard_policy_path(self) -> Path | None:
        if not self.guard_policy_path:
            return None
        path = Path(self.guard_policy_path)
        return path if path.is_absolute() else REPO_ROOT / path


def export_env_file(path: Path = ENV_FILE) -> None:
    """Export the root .env into os.environ: LiteLLM reads provider keys (OPENAI_API_KEY, ...)
    from the environment, not from Settings. Real env vars win, and blank values are skipped
    so `OPENAI_API_KEY=` in the template can't mask a key set elsewhere."""
    if not path.is_file():
        return
    for key, value in dotenv_values(path).items():
        if value and key not in os.environ:
            os.environ[key] = value


@lru_cache
def get_settings() -> Settings:
    export_env_file()
    return Settings()
