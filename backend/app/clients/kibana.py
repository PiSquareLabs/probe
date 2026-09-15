"""Kibana actions API client — the half of Elastic that can *create* Jira issues.

The `.jira` action connector is registered with ``minimumLicenseRequired: 'gold'``
(elastic/kibana#67178), and Gold is discontinued for new customers. So on a
free/Basic stack this client's ``create_jira_connector`` will fail the licence
check. That is expected: :meth:`connector_type_support` reports it up front so
the app can route ticket creation directly to Jira instead of guessing.
"""

from __future__ import annotations

from typing import Any

from ..errors import LicenseError, UpstreamError
from ..models import ElasticCredentials, JiraCredentials
from .base import BaseClient
from .elastic import auth_headers

JIRA_ACTION_TYPE_ID = ".jira"


class KibanaClient(BaseClient):
    service = "Kibana"

    def __init__(self, creds: ElasticCredentials, *, api_key: str = ""):
        shadow = creds.model_copy(update={"api_key": api_key or creds.api_key})
        headers, auth = auth_headers(shadow)
        # Kibana rejects every non-GET request without this header.
        headers["kbn-xsrf"] = "true"
        super().__init__(
            creds.kibana_url, headers=headers, auth=auth, verify=creds.verify_tls
        )
        self.creds = creds

    async def status(self) -> dict[str, Any]:
        data = await self.get_json("/api/status")
        return {
            "name": data.get("name"),
            "version": (data.get("version") or {}).get("number"),
            "state": ((data.get("status") or {}).get("overall") or {}).get("level"),
        }

    # -- licence probing ---------------------------------------------------
    async def connector_types(self) -> list[dict[str, Any]]:
        return await self.get_json("/api/actions/connector_types")

    async def connector_type_support(
        self, type_id: str = JIRA_ACTION_TYPE_ID
    ) -> dict[str, Any]:
        """Ask Kibana whether this connector type is usable on this licence.

        Returns ``{supported, enabled, enabled_in_license, minimum_license_required}``.
        A missing type means the plugin is disabled altogether rather than
        licence-gated, which is a different fix, so the two are distinguished.
        """
        try:
            types = await self.connector_types()
        except UpstreamError:
            return {
                "supported": False,
                "enabled": False,
                "enabled_in_license": False,
                "minimum_license_required": "gold",
                "reason": "Kibana could not be reached to check connector licensing.",
            }

        for entry in types:
            if entry.get("id") == type_id:
                enabled_in_license = bool(entry.get("enabled_in_license"))
                return {
                    "supported": True,
                    "enabled": bool(entry.get("enabled")),
                    "enabled_in_license": enabled_in_license,
                    "minimum_license_required": entry.get(
                        "minimum_license_required", "gold"
                    ),
                    "name": entry.get("name"),
                    "reason": (
                        "Available on this licence."
                        if enabled_in_license
                        else "The current Elastic licence does not include this "
                        "connector type."
                    ),
                }

        return {
            "supported": False,
            "enabled": False,
            "enabled_in_license": False,
            "minimum_license_required": "gold",
            "reason": f"Kibana does not expose the {type_id} connector type.",
        }

    # -- connector lifecycle ----------------------------------------------
    async def find_connector(self, name: str) -> dict[str, Any] | None:
        connectors = await self.get_json("/api/actions/connectors")
        for connector in connectors:
            if connector.get("name") == name:
                return connector
        return None

    async def create_jira_connector(
        self, connector_id: str, name: str, jira: JiraCredentials
    ) -> dict[str, Any]:
        """Create the `.jira` action connector from credentials held by our app."""
        response = await self.request(
            "POST",
            f"/api/actions/connector/{connector_id}",
            json_body={
                "name": name,
                "connector_type_id": JIRA_ACTION_TYPE_ID,
                "config": {
                    "apiUrl": jira.base_url,
                    "projectKey": jira.project_key,
                },
                "secrets": {
                    "email": jira.account_email,
                    "apiToken": jira.api_token,
                },
            },
            allow_statuses=(400, 402, 403, 409),
        )

        if response.status_code == 409:
            existing = await self.find_connector(name)
            if existing:
                return existing

        if response.status_code in (400, 402, 403):
            body = response.json() if response.content else {}
            message = str(body.get("message", "")).lower()
            if "license" in message or response.status_code == 402:
                raise LicenseError(
                    "The Elastic licence does not permit the Jira action connector.",
                    required="gold",
                )
            raise UpstreamError(
                self.service,
                f"Kibana refused to create the Jira connector: "
                f"{body.get('message') or response.status_code}",
                status=response.status_code,
                detail=body,
            )

        if response.status_code >= 400:
            raise UpstreamError(
                self.service,
                "Kibana refused to create the Jira connector.",
                status=response.status_code,
            )
        return response.json()

    async def delete_connector(self, connector_id: str) -> bool:
        response = await self.request(
            "DELETE", f"/api/actions/connector/{connector_id}", allow_statuses=(404,)
        )
        return response.status_code != 404

    # -- execution ---------------------------------------------------------
    async def execute(self, connector_id: str, params: dict[str, Any]) -> dict[str, Any]:
        result = await self.post_json(
            f"/api/actions/connector/{connector_id}/_execute", {"params": params}
        )
        if result.get("status") == "error":
            raise UpstreamError(
                self.service,
                result.get("message") or "The Kibana connector returned an error.",
                detail=result.get("service_message"),
            )
        return result

    async def create_issue(
        self,
        connector_id: str,
        *,
        summary: str,
        description: str = "",
        issue_type: str | None = None,
        priority: str | None = None,
        labels: list[str] | None = None,
        parent: str | None = None,
    ) -> dict[str, Any]:
        """Create a Jira issue through the action connector.

        Note this path talks Jira REST **v2** underneath, where ``description``
        is a plain string — no ADF conversion, unlike the direct v3 path.
        """
        incident: dict[str, Any] = {"summary": summary, "description": description or ""}
        if issue_type:
            incident["issueType"] = issue_type
        if priority:
            incident["priority"] = priority
        if labels:
            incident["labels"] = [l.strip().replace(" ", "-") for l in labels if l.strip()]
        if parent:
            incident["parent"] = parent

        result = await self.execute(
            connector_id,
            {"subAction": "pushToService", "subActionParams": {"incident": incident, "comments": []}},
        )
        data = result.get("data") or {}
        return {
            "id": data.get("id"),
            "key": data.get("title"),  # the connector returns the issue key as `title`
            "url": data.get("url"),
        }

    async def issue_types(self, connector_id: str) -> list[dict[str, Any]]:
        result = await self.execute(
            connector_id, {"subAction": "issueTypes", "subActionParams": {}}
        )
        return result.get("data") or []
