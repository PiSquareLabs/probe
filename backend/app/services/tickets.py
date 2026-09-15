"""Ticket creation and duplicate detection.

One public entry point, two execution paths:

* ``elastic_connector`` — Kibana's `.jira` action connector does the write.
* ``direct_jira``       — this app calls ``POST /rest/api/3/issue`` itself.

Callers pick ``auto`` and get whichever the licence allows. Before writing, the
synced Elasticsearch index is searched for near-duplicates, which is the reason
the content connector is worth provisioning at all.
"""

from __future__ import annotations

import logging

from ..clients.elastic import ElasticClient
from ..clients.jira import JiraClient, escape_jql
from ..clients.kibana import KibanaClient
from ..errors import UpstreamError
from ..models import (
    ConnectorSettings,
    JiraCredentials,
    SearchResponse,
    SimilarTicket,
    TicketRequest,
    TicketResponse,
)
from ..services.provisioning import ACTION_CONNECTOR_ID

log = logging.getLogger("probe.tickets")

# A hit has to clear this to be called a likely duplicate rather than merely
# related. Lucene scores are not normalised, so this is a heuristic threshold
# tuned to "shares several distinctive terms with the summary".
DUPLICATE_SCORE_THRESHOLD = 8.0


def _with_urls(tickets: list[SimilarTicket], base_url: str) -> list[SimilarTicket]:
    """Give every hit a clickable link.

    The connector does not always index a ``url`` field, and a duplicate
    warning the reviewer cannot click through to is close to useless.
    """
    for ticket in tickets:
        if not ticket.url and ticket.key:
            ticket.url = f"{base_url}/browse/{ticket.key}"
    return tickets


async def find_similar(
    *,
    es: ElasticClient,
    jira_client: JiraClient,
    index_name: str,
    query: str,
    project_key: str,
    size: int = 5,
) -> tuple[list[SimilarTicket], str]:
    """Look for existing tickets like ``query``.

    Prefers the Elasticsearch index — it is faster, fuzzier and does not spend
    Jira rate limit. Falls back to JQL when the index is missing or empty,
    which is the normal state before the first connector sync completes.
    """
    try:
        total, hits = await es.search_tickets(
            index_name, query, size=size, project_key=project_key
        )
        if hits:
            return _with_urls(hits, jira_client.creds.base_url), "elasticsearch"
    except UpstreamError as exc:
        log.warning("Elasticsearch duplicate search failed, falling back to JQL: %s", exc)

    try:
        jql = f'project = "{escape_jql(project_key)}" AND text ~ "{escape_jql(query)}" ORDER BY created DESC'
        return await jira_client.search(jql, limit=size), "jira"
    except UpstreamError as exc:
        log.warning("Jira duplicate search failed: %s", exc)
        return [], "jira"


async def search_tickets(
    *,
    es: ElasticClient,
    jira_client: JiraClient,
    index_name: str,
    query: str,
    project_key: str,
    size: int,
) -> SearchResponse:
    try:
        total, hits = await es.search_tickets(
            index_name, query, size=size, project_key=project_key
        )
        if hits:
            return SearchResponse(
                source="elasticsearch",
                total=total,
                results=_with_urls(hits, jira_client.creds.base_url),
            )
        note = (
            f"Index '{index_name}' returned no results. If the connector has not "
            "synced yet, results below come from Jira directly."
        )
    except UpstreamError as exc:
        note = f"Elasticsearch search unavailable ({exc.message}); queried Jira instead."

    if query.strip():
        jql = f'project = "{escape_jql(project_key)}" AND text ~ "{escape_jql(query)}" ORDER BY created DESC'
    else:
        jql = f'project = "{escape_jql(project_key)}" ORDER BY created DESC'
    results = await jira_client.search(jql, limit=size)
    return SearchResponse(
        source="jira", total=len(results), results=results, note=note
    )


async def create_ticket(
    *,
    request: TicketRequest,
    jira: JiraCredentials,
    connector: ConnectorSettings,
    es: ElasticClient,
    jira_client: JiraClient,
    kibana: KibanaClient,
    route: str,
    action_connector_id: str = ACTION_CONNECTOR_ID,
) -> TicketResponse:
    project_key = request.project_key or jira.project_key
    issue_type = request.issue_type or jira.default_issue_type

    duplicates: list[SimilarTicket] = []
    if request.check_duplicates:
        candidates, _source = await find_similar(
            es=es,
            jira_client=jira_client,
            index_name=connector.index_name,
            query=request.summary,
            project_key=project_key,
        )
        duplicates = [
            c
            for c in candidates
            if c.score is None or c.score >= DUPLICATE_SCORE_THRESHOLD
        ][:5]

    if route == "elastic_connector":
        try:
            created = await kibana.create_issue(
                action_connector_id,
                summary=request.summary,
                description=request.description,
                issue_type=issue_type,
                priority=request.priority,
                labels=request.labels,
                parent=request.parent,
            )
            route_used = "elastic_connector"
        except UpstreamError as exc:
            # The connector can fail for reasons unrelated to the licence (a
            # rotated secret, Kibana restarting). A ticket the user asked for
            # matters more than which component created it, so fall through.
            log.warning(
                "Kibana connector create failed (%s); retrying via the Jira API.",
                exc.message,
            )
            created = await _create_direct(jira_client, project_key, issue_type, request)
            route_used = "direct_jira"
    else:
        created = await _create_direct(jira_client, project_key, issue_type, request)
        route_used = "direct_jira"

    key = created.get("key")
    return TicketResponse(
        created=True,
        key=key,
        id=created.get("id"),
        url=created.get("url") or (f"{jira.base_url}/browse/{key}" if key else None),
        route_used=route_used,
        duplicates=duplicates,
        message=(
            f"Created {key} in {project_key} via "
            + (
                "the Kibana Jira connector."
                if route_used == "elastic_connector"
                else "the Jira REST API."
            )
        ),
    )


async def _create_direct(
    jira_client: JiraClient, project_key: str, issue_type: str, request: TicketRequest
) -> dict:
    return await jira_client.create_issue(
        project_key=project_key,
        summary=request.summary,
        description=request.description,
        issue_type=issue_type,
        priority=request.priority,
        labels=request.labels,
        parent=request.parent,
    )
