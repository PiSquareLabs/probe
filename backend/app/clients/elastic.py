"""Elasticsearch client: cluster info, licence, content-connector lifecycle, search.

This covers the *content* connector (``service_type: jira``) — the one that
pulls Jira issues into an index. Creating Jira issues is Kibana's job; see
``kibana.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..errors import UpstreamError
from ..models import ConnectorSettings, ElasticCredentials, JiraCredentials, SimilarTicket
from .base import BaseClient


def auth_headers(creds: ElasticCredentials) -> tuple[dict[str, str], tuple[str, str] | None]:
    """Prefer an API key; fall back to Basic auth."""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if creds.api_key:
        headers["Authorization"] = f"ApiKey {creds.api_key}"
        return headers, None
    if creds.username:
        return headers, (creds.username, creds.password)
    return headers, None


class ElasticClient(BaseClient):
    service = "Elasticsearch"

    def __init__(self, creds: ElasticCredentials):
        headers, auth = auth_headers(creds)
        super().__init__(creds.es_url, headers=headers, auth=auth, verify=creds.verify_tls)
        self.creds = creds

    # -- cluster -----------------------------------------------------------
    async def info(self) -> dict[str, Any]:
        response = await self.request("GET", "/", allow_statuses=(401, 403))
        if response.status_code in (401, 403):
            raise UpstreamError(
                self.service,
                f"Elasticsearch rejected the credentials ({response.status_code}).",
                status=response.status_code,
                hint="Check ELASTIC_API_KEY, or the username and password.",
            )
        return response.json()

    async def license(self) -> dict[str, Any]:
        """Return the licence, or an empty dict when it cannot be read.

        A missing licence endpoint is not fatal — it only means the app cannot
        pre-judge whether the Kibana Jira action connector will be permitted,
        and will find out when it tries.
        """
        response = await self.request("GET", "/_license", allow_statuses=(400, 403, 404))
        if response.status_code >= 400:
            return {}
        return response.json().get("license", {})

    # -- connector lifecycle ----------------------------------------------
    async def get_connector(self, connector_id: str) -> dict[str, Any] | None:
        response = await self.request(
            "GET", f"/_connector/{connector_id}", allow_statuses=(404,)
        )
        if response.status_code == 404:
            return None
        return response.json()

    async def create_connector(
        self, settings: ConnectorSettings, service_type: str = "jira"
    ) -> dict[str, Any]:
        return await self.put_json(
            f"/_connector/{settings.connector_id}",
            {
                "index_name": settings.index_name,
                "name": settings.name,
                "service_type": service_type,
                "is_native": False,
            },
            expected=(200, 201),
        )

    async def update_configuration(
        self, connector_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Push configuration values into a connector.

        Elasticsearch stores ``configuration`` as ``{field: {value: ...}}`` but
        the update API takes a flat ``{"values": {field: value}}``.
        """
        return await self.put_json(
            f"/_connector/{connector_id}/_configuration", {"values": values}
        )

    async def update_scheduling(
        self, connector_id: str, *, interval: str, enabled: bool = True
    ) -> dict[str, Any]:
        return await self.put_json(
            f"/_connector/{connector_id}/_scheduling",
            {"scheduling": {"full": {"enabled": enabled, "interval": interval}}},
        )

    async def update_api_key_id(self, connector_id: str, api_key_id: str) -> dict[str, Any]:
        # api_key_secret_id is for Elastic-managed connectors only; self-managed
        # connectors read the key from their own config.yml.
        return await self.put_json(
            f"/_connector/{connector_id}/_api_key_id", {"api_key_id": api_key_id}
        )

    async def start_sync(self, connector_id: str, job_type: str = "full") -> dict[str, Any]:
        return await self.post_json(
            "/_connector/_sync_job",
            {"id": connector_id, "job_type": job_type},
            expected=(200, 201),
        )

    async def last_sync_job(self, connector_id: str) -> dict[str, Any] | None:
        response = await self.request(
            "GET",
            "/_connector/_sync_job",
            params={"connector_id": connector_id, "size": 1},
            allow_statuses=(404,),
        )
        if response.status_code == 404:
            return None
        results = response.json().get("results", [])
        return results[0] if results else None

    async def wait_for_configuration(
        self, connector_id: str, *, attempts: int = 5, delay: float = 2.0
    ) -> dict[str, Any] | None:
        """Wait for the connector service to register its configuration schema.

        The schema is published by the running ``elastic-connectors`` service,
        not by Elasticsearch. Writing configuration values before it checks in
        silently does nothing, so callers need to know whether it is up.
        """
        for attempt in range(attempts):
            connector = await self.get_connector(connector_id)
            if connector and connector.get("configuration"):
                return connector
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
        return None

    # -- index -------------------------------------------------------------
    async def index_exists(self, index: str) -> bool:
        response = await self.request("HEAD", index_path(index), allow_statuses=(404,))
        return response.status_code == 200

    async def document_count(self, index: str) -> int | None:
        response = await self.request(
            "GET", f"{index_path(index)}/_count", allow_statuses=(404,)
        )
        if response.status_code == 404:
            return None
        return response.json().get("count")

    async def search_tickets(
        self, index: str, query: str, *, size: int = 10, project_key: str | None = None
    ) -> tuple[int, list[SimilarTicket]]:
        """Full-text search over the synced Jira content."""
        if query.strip():
            must: dict[str, Any] = {
                "multi_match": {
                    "query": query,
                    # Field names follow the Jira connector's document shape;
                    # the wildcards keep this resilient to schema drift.
                    "fields": ["summary^3", "title^3", "description", "body", "*"],
                    "type": "best_fields",
                    "fuzziness": "AUTO",
                }
            }
        else:
            must = {"match_all": {}}

        body: dict[str, Any] = {"size": size, "query": must}
        if project_key:
            body["query"] = {
                "bool": {
                    "must": [must],
                    "filter": [
                        {
                            "bool": {
                                "should": [
                                    {"term": {"project_key": project_key}},
                                    {"term": {"project.key": project_key}},
                                    {"prefix": {"key": f"{project_key}-"}},
                                ],
                                "minimum_should_match": 1,
                            }
                        }
                    ],
                }
            }

        response = await self.request(
            "POST", f"{index_path(index)}/_search", json_body=body, allow_statuses=(404,)
        )
        if response.status_code == 404:
            return 0, []

        data = response.json()
        total = (data.get("hits", {}).get("total") or {}).get("value", 0)
        return total, [_hit_to_ticket(h) for h in data.get("hits", {}).get("hits", [])]

    # -- security ----------------------------------------------------------
    async def create_connector_api_key(
        self, *, name: str, index_name: str
    ) -> dict[str, Any]:
        """Mint the API key the self-managed connector service authenticates with.

        Privileges mirror what the connector framework needs: manage its own
        connector documents and sync jobs, and write to its target index.
        """
        return await self.post_json(
            "/_security/api_key",
            {
                "name": name,
                "role_descriptors": {
                    "probe-connector-role": {
                        "cluster": ["monitor", "manage_connector"],
                        "indices": [
                            {
                                "names": [
                                    index_name,
                                    f".search-acl-filter-{index_name}",
                                    ".elastic-connectors*",
                                ],
                                "privileges": ["all"],
                                "allow_restricted_indices": False,
                            }
                        ],
                    }
                },
            },
            expected=(200, 201),
        )


def index_path(index: str) -> str:
    """URL-safe index path. Guards against an index name injecting a path."""
    from urllib.parse import quote

    return "/" + quote(index, safe="")


def _hit_to_ticket(hit: dict[str, Any]) -> SimilarTicket:
    src = hit.get("_source") or {}
    key = src.get("key") or src.get("issue_key") or src.get("id")
    return SimilarTicket(
        key=str(key) if key is not None else None,
        summary=src.get("summary") or src.get("title") or src.get("name"),
        status=_nested(src, "status") or _nested(src, "issue_status"),
        url=src.get("url") or src.get("_url") or src.get("link"),
        score=hit.get("_score"),
        source="elasticsearch",
    )


def _nested(src: dict[str, Any], field: str) -> str | None:
    """Read a field that the connector may index as a string or an object."""
    value = src.get(field)
    if isinstance(value, dict):
        return value.get("name") or value.get("value")
    return value if isinstance(value, str) else None


def build_connector_configuration(
    jira: JiraCredentials, settings: ConnectorSettings
) -> dict[str, Any]:
    """Map the credentials gathered in our UI onto the Jira connector's fields.

    This function is the crux of the product: the operator fills in one Jira
    form, and these are the connector settings they never have to touch in
    Kibana.
    """
    return {
        "data_source": "jira_cloud",
        "jira_url": jira.base_url,
        "account_email": jira.account_email,
        "api_token": jira.api_token,
        "projects": settings.projects or "*",
        "ssl_enabled": settings.ssl_enabled,
        "retry_count": settings.retry_count,
        "concurrent_downloads": settings.concurrent_downloads,
        # Document-level security is a paid-tier feature; keep it off so the
        # connector works on a Basic licence.
        "use_document_level_security": False,
        "use_text_extraction_service": False,
    }
