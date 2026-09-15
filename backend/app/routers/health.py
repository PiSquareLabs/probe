"""Liveness and a coarse readiness view for the UI's status bar."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_store
from ..store import ConnectionStore

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "probe-backend"}


@router.get("/api/health")
async def api_health(store: ConnectionStore = Depends(get_store)) -> dict:
    return {"status": "ok", "configured": store.is_configured()}
