# syntax=docker/dockerfile:1.7
# The agent API (FastAPI) with the guard, its ML detectors, the sandbox tool server and the
# playground's attack mode. Built from the repo root:
#   docker build -f apps/agent/Dockerfile -t boundary-agent .
# Dependencies come from uv.lock (CPU-only PyTorch on Linux), so the image matches what CI tests.
# Models are not baked in: they download on first start into the HF_HOME volume (~2 GB; the gated
# Llama Prompt Guard needs HF_TOKEN), so the image stays small and the cache survives upgrades.

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.2 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    HF_HOME=/data/hf \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false \
    BOUNDARY_SANDBOX_ROOT=/data/mcp-sandbox

WORKDIR /app

# 1) Third-party dependencies only, from the workspace metadata: cached until uv.lock changes.
COPY pyproject.toml uv.lock .python-version ./
COPY apps/agent/pyproject.toml apps/agent/README.md apps/agent/
COPY apps/sandbox-mcp/pyproject.toml apps/sandbox-mcp/README.md apps/sandbox-mcp/
COPY packages/guard/pyproject.toml packages/guard/README.md packages/guard/
COPY packages/eval/pyproject.toml packages/eval/README.md packages/eval/
COPY packages/detector/pyproject.toml packages/detector/README.md packages/detector/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace \
      --package boundary-agent --extra ml --extra playground

# 2) Our code, policies and the eval assets attack mode replays (scenarios, fixtures, cassette).
COPY apps/agent apps/agent
COPY apps/sandbox-mcp apps/sandbox-mcp
COPY packages/guard packages/guard
COPY packages/eval packages/eval
COPY policies policies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package boundary-agent --extra ml --extra playground

# Run as an unprivileged user; /data holds the model cache and the sandbox (mounted volumes).
# COPY keeps the build machine's permissions (a checkout can have owner-only directories), so make
# our sources world-readable; the code stays root-owned and read-only for the app user.
RUN chmod -R a+rX apps packages policies \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data/hf /data/mcp-sandbox \
    && chown -R app:app /data
USER app

EXPOSE 8000
# Model download + load on first start can take several minutes; later starts take ~1 minute.
HEALTHCHECK --interval=30s --timeout=5s --start-period=600s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["boundary", "serve", "--host", "0.0.0.0", "--port", "8000"]
