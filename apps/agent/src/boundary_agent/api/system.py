"""Liveness and Prometheus metrics."""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape endpoint. The proxy never serves it (infra/Caddyfile)."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
