"""Ticket creation and search."""

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
from ..models import (
    ConnectorSettings,
    JiraCredentials,
    SearchRequest,
    SearchResponse,
    TicketRequest,
    TicketResponse,
)
from ..services.capabilities import resolve_route
from ..services.tickets import create_ticket, find_similar, search_tickets
from ..store import ConnectionStore

router = APIRouter(prefix="/api/tickets", tags=["tickets"], dependencies=[Depends(require_api_key)])


async def _effective_route(
    requested: str | None, store: ConnectionStore, kibana: KibanaClient
) -> str:
    preferred = requested or store.ticket_route()
    if preferred == "direct_jira":
        return "direct_jira"
    support = await kibana.connector_type_support()
    available = bool(support.get("enabled_in_license")) and bool(support.get("enabled"))
    return resolve_route(preferred, available)


@router.post("", response_model=TicketResponse)
async def create(
    request: TicketRequest,
    es: ElasticClient = Depends(elastic_client),
    jira_api: JiraClient = Depends(jira_client),
    kibana: KibanaClient = Depends(kibana_client),
    jira: JiraCredentials = Depends(stored_jira),
    connector: ConnectorSettings = Depends(stored_connector),
    store: ConnectionStore = Depends(get_store),
) -> TicketResponse:
    """Create a Jira ticket via whichever path this licence supports."""
    route = await _effective_route(request.route, store, kibana)
    return await create_ticket(
        request=request,
        jira=jira,
        connector=connector,
        es=es,
        jira_client=jira_api,
        kibana=kibana,
        route=route,
    )


@router.post("/search", response_model=SearchResponse)
async def search(
    request: SearchRequest,
    es: ElasticClient = Depends(elastic_client),
    jira_api: JiraClient = Depends(jira_client),
    jira: JiraCredentials = Depends(stored_jira),
    connector: ConnectorSettings = Depends(stored_connector),
) -> SearchResponse:
    """Search synced tickets, falling back to Jira when the index is not ready."""
    return await search_tickets(
        es=es,
        jira_client=jira_api,
        index_name=connector.index_name,
        query=request.query,
        project_key=request.project_key or jira.project_key,
        size=request.size,
    )


@router.post("/similar", response_model=SearchResponse)
async def similar(
    request: SearchRequest,
    es: ElasticClient = Depends(elastic_client),
    jira_api: JiraClient = Depends(jira_client),
    jira: JiraCredentials = Depends(stored_jira),
    connector: ConnectorSettings = Depends(stored_connector),
) -> SearchResponse:
    """Pre-flight duplicate check, called as the user types a summary."""
    results, source = await find_similar(
        es=es,
        jira_client=jira_api,
        index_name=connector.index_name,
        query=request.query,
        project_key=request.project_key or jira.project_key,
        size=request.size,
    )
    return SearchResponse(source=source, total=len(results), results=results)
