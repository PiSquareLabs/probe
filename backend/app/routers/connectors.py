"""Connector provisioning and status — the "configure ES from our UI" surface."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..clients.elastic import ElasticClient
from ..clients.jira import JiraClient
from ..clients.kibana import KibanaClient
from ..deps import (
    elastic_client,
    get_store,
    jira_client,
    kibana_client,
    require_api_key,
    stored_connector,
    stored_jira,
)
from ..errors import UpstreamError
from ..models import (
    CapabilityReport,
    ConnectorSettings,
    JiraCredentials,
    ProvisionRequest,
    ProvisionResult,
    SyncStatus,
)
from ..services.capabilities import build_capability_report
from ..services.provisioning import provision, render_connector_config
from ..store import ConnectionStore

router = APIRouter(
    prefix="/api/connector", tags=["connector"], dependencies=[Depends(require_api_key)]
)


@router.get("/capabilities", response_model=CapabilityReport)
async def capabilities(
    es: ElasticClient = Depends(elastic_client),
    kibana: KibanaClient = Depends(kibana_client),
    store: ConnectionStore = Depends(get_store),
) -> CapabilityReport:
    """What this deployment's licence permits, and which path tickets will take."""
    return await build_capability_report(es, kibana, preferred_route=store.ticket_route())


@router.post("/provision", response_model=ProvisionResult)
async def provision_connectors(
    request: ProvisionRequest = ProvisionRequest(),
    es: ElasticClient = Depends(elastic_client),
    kibana: KibanaClient = Depends(kibana_client),
    jira_api: JiraClient = Depends(jira_client),
    jira: JiraCredentials = Depends(stored_jira),
    settings: ConnectorSettings = Depends(stored_connector),
    store: ConnectionStore = Depends(get_store),
) -> ProvisionResult:
    """Configure both Elastic integrations from the stored Jira credentials."""
    return await provision(
        es=es,
        kibana=kibana,
        jira_client=jira_api,
        jira=jira,
        settings=settings,
        request=request,
        preferred_route=store.ticket_route(),
    )


@router.get("/status", response_model=SyncStatus)
async def connector_status(
    es: ElasticClient = Depends(elastic_client),
    settings: ConnectorSettings = Depends(stored_connector),
) -> SyncStatus:
    connector = await es.get_connector(settings.connector_id)
    if not connector:
        return SyncStatus(
            connector_id=settings.connector_id,
            index_name=settings.index_name,
            status="not_created",
        )

    last_job = None
    try:
        last_job = await es.last_sync_job(settings.connector_id)
    except UpstreamError:
        pass

    index_name = connector.get("index_name") or settings.index_name
    document_count = None
    try:
        document_count = await es.document_count(index_name)
    except UpstreamError:
        pass

    return SyncStatus(
        connector_id=settings.connector_id,
        index_name=index_name,
        status=connector.get("status"),
        service_type=connector.get("service_type"),
        last_sync_status=connector.get("last_sync_status"),
        last_sync_error=connector.get("last_sync_error"),
        last_synced=connector.get("last_synced"),
        last_seen=connector.get("last_seen"),
        configured=bool(connector.get("configuration")),
        docs_indexed=(last_job or {}).get("indexed_document_count"),
        document_count=document_count,
    )


@router.post("/sync", response_model=dict)
async def trigger_sync(
    job_type: str = "full",
    es: ElasticClient = Depends(elastic_client),
    settings: ConnectorSettings = Depends(stored_connector),
) -> dict:
    result = await es.start_sync(settings.connector_id, job_type=job_type)
    return {"queued": True, "job_type": job_type, "detail": result}


@router.get("/service-config", response_model=dict)
async def service_config(
    es: ElasticClient = Depends(elastic_client),
    settings: ConnectorSettings = Depends(stored_connector),
) -> dict:
    """The config.yml for the self-managed elastic-connectors service.

    The API key is not re-issued here — Elasticsearch only returns a key's
    secret at creation time — so the placeholder stands until the operator
    pastes in the one from provisioning.
    """
    return {
        "filename": "connectors-config/config.yml",
        "content": render_connector_config(
            es_url=es.creds.es_url,
            connector_id=settings.connector_id,
            api_key="<paste the api key returned by /api/connector/provision>",
        ),
    }
