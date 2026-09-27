FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY apps/agent/pyproject.toml apps/agent/README.md /app/api/
COPY apps/sandbox-mcp/pyproject.toml apps/sandbox-mcp/README.md /app/mcp_server/
COPY apps/agent/src /app/api/src
COPY apps/sandbox-mcp/src /app/mcp_server/src
COPY apps/sandbox-mcp/sandbox/.gitkeep /app/mcp_server/sandbox/.gitkeep

RUN pip install --no-cache-dir -e /app/mcp_server -e /app/api

EXPOSE 8000

CMD ["uvicorn", "boundary_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/app/api/src"]
