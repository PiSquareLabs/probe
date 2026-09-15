"""FastAPI dependencies: settings, store, auth and upstream clients."""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from fastapi import Depends, Header

from .clients.elastic import ElasticClient
from .clients.jira import JiraClient
from .clients.kibana import KibanaClient
from .config import Settings, get_settings
from .crypto import SecretBox
from .errors import NotConfiguredError, ProbeError
from .models import ConnectorSettings, ElasticCredentials, JiraCredentials
from .store import ConnectionStore


class UnauthorizedError(ProbeError):
    status_code = 401
    code = "unauthorized"


@lru_cache
def get_store() -> ConnectionStore:
    settings = get_settings()
    return ConnectionStore(settings.probe_state_path, SecretBox(settings.probe_secret_key))


async def require_api_key(
    x_probe_key: str | None = Header(default=None, alias="X-Probe-Key"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Guard the API with a shared secret.

    When PROBE_API_KEY is unset the guard is inert, which keeps first-run local
    development friction-free. Any deployment that sets it gets enforcement.
    """
    if not settings.probe_api_key:
        return
    if x_probe_key != settings.probe_api_key:
        raise UnauthorizedError(
            "Missing or invalid X-Probe-Key header.",
            hint="Set VITE_PROBE_API_KEY in the frontend to match PROBE_API_KEY.",
        )


def stored_jira(store: ConnectionStore = Depends(get_store)) -> JiraCredentials:
    creds = store.jira()
    if not creds:
        raise NotConfiguredError(
            "Jira is not configured yet.", hint="Complete setup at POST /api/setup."
        )
    return creds


def stored_elastic(store: ConnectionStore = Depends(get_store)) -> ElasticCredentials:
    creds = store.elastic()
    if not creds:
        raise NotConfiguredError(
            "Elasticsearch is not configured yet.",
            hint="Complete setup at POST /api/setup.",
        )
    return creds


def stored_connector(store: ConnectionStore = Depends(get_store)) -> ConnectorSettings:
    return store.connector() or ConnectorSettings()


async def jira_client(
    creds: JiraCredentials = Depends(stored_jira),
    elastic: ElasticCredentials = Depends(stored_elastic),
) -> AsyncIterator[JiraClient]:
    client = JiraClient(creds, verify=elastic.verify_tls)
    try:
        yield client
    finally:
        await client.aclose()


async def elastic_client(
    creds: ElasticCredentials = Depends(stored_elastic),
) -> AsyncIterator[ElasticClient]:
    client = ElasticClient(creds)
    try:
        yield client
    finally:
        await client.aclose()


async def kibana_client(
    creds: ElasticCredentials = Depends(stored_elastic),
    settings: Settings = Depends(get_settings),
) -> AsyncIterator[KibanaClient]:
    client = KibanaClient(creds, api_key=settings.kibana_api_key)
    try:
        yield client
    finally:
        await client.aclose()
