"""Jira Cloud REST API v3 client.

Authentication is HTTP Basic with ``email:api_token``. Per Atlassian's docs,
OAuth scopes are *not* enforced for API-token Basic auth: the token inherits the
account's own permissions. So the meaningful pre-flight is a project-permission
check (``BROWSE_PROJECTS`` + ``CREATE_ISSUES``), not a scope check.
"""

from __future__ import annotations

import base64
from typing import Any

from ..errors import UpstreamError
from ..models import JiraCredentials, SimilarTicket
from .base import BaseClient

CREATE_PERMISSIONS = ("BROWSE_PROJECTS", "CREATE_ISSUES")

# Fields worth pulling back on a search; asking for everything is wasteful and
# Jira charges the same rate limit either way.
SEARCH_FIELDS = "summary,status,issuetype,priority,created,updated"


def text_to_adf(text: str) -> dict[str, Any]:
    """Convert plain text to Atlassian Document Format.

    REST v3 rejects a bare string for ``description``. Blank lines separate
    paragraphs; everything else is passed through as text.
    """
    paragraphs = [p for p in (text or "").split("\n\n")]
    content: list[dict[str, Any]] = []
    for para in paragraphs:
        stripped = para.strip()
        if not stripped:
            continue
        content.append(
            {"type": "paragraph", "content": [{"type": "text", "text": stripped}]}
        )
    if not content:
        content = [{"type": "paragraph", "content": []}]
    return {"type": "doc", "version": 1, "content": content}


def adf_to_text(node: Any) -> str:
    """Flatten an ADF document back to plain text for display."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        inner = adf_to_text(node.get("content"))
        if node.get("type") in {"paragraph", "heading"}:
            return inner + "\n"
        return inner
    return ""


class JiraClient(BaseClient):
    service = "Jira"

    def __init__(self, creds: JiraCredentials, *, verify: bool | str = True):
        token = base64.b64encode(
            f"{creds.account_email}:{creds.api_token}".encode("utf-8")
        ).decode("ascii")
        super().__init__(
            creds.base_url,
            headers={
                "Authorization": f"Basic {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "probe-jira-integration/1.0",
            },
            verify=verify,
        )
        self.creds = creds

    # -- identity ----------------------------------------------------------
    async def myself(self) -> dict[str, Any]:
        """Validate the credentials and resolve the acting account."""
        response = await self.request(
            "GET", "/rest/api/3/myself", allow_statuses=(401, 403, 404)
        )
        if response.status_code == 401:
            raise UpstreamError(
                self.service,
                "Jira rejected the credentials (401).",
                status=401,
                hint=(
                    "Check the account email and that the API token is current. "
                    "Tokens are managed at "
                    "https://id.atlassian.com/manage-profile/security/api-tokens"
                ),
            )
        if response.status_code == 403:
            raise UpstreamError(
                self.service,
                "Jira accepted the credentials but refused the request (403).",
                status=403,
                hint="The account may be deactivated or blocked by an IP allowlist.",
            )
        if response.status_code == 404:
            raise UpstreamError(
                self.service,
                "No Jira REST API at that base URL (404).",
                status=404,
                hint="Expected something like https://your-org.atlassian.net",
            )
        return response.json()

    # -- discovery ---------------------------------------------------------
    async def projects(self, *, query: str = "", limit: int = 50) -> list[dict]:
        params: dict[str, Any] = {"maxResults": limit, "orderBy": "key"}
        if query:
            params["query"] = query
        data = await self.get_json("/rest/api/3/project/search", params=params)
        return [
            {
                "id": p.get("id"),
                "key": p.get("key"),
                "name": p.get("name"),
                "project_type": p.get("projectTypeKey"),
            }
            for p in data.get("values", [])
        ]

    async def issue_types(self, project_key: str) -> list[dict]:
        """Issue types creatable in a project.

        Prefers the current ``createmeta/{key}/issuetypes`` endpoint and falls
        back to the deprecated flat ``createmeta`` for older Jira versions.
        """
        response = await self.request(
            "GET",
            f"/rest/api/3/issue/createmeta/{project_key}/issuetypes",
            allow_statuses=(404, 410),
        )
        if response.status_code in (404, 410):
            data = await self.get_json(
                "/rest/api/3/issue/createmeta",
                params={"projectKeys": project_key, "expand": "projects.issuetypes"},
            )
            projects = data.get("projects") or []
            values = projects[0].get("issuetypes", []) if projects else []
        else:
            values = response.json().get("values", [])

        return [
            {
                "id": it.get("id"),
                "name": it.get("name"),
                "subtask": bool(it.get("subtask")),
                "description": it.get("description"),
            }
            for it in values
        ]

    async def check_permissions(self, project_key: str) -> dict[str, bool]:
        """Confirm the account can actually create issues in the project."""
        data = await self.get_json(
            "/rest/api/3/mypermissions",
            params={
                "projectKey": project_key,
                "permissions": ",".join(CREATE_PERMISSIONS),
            },
        )
        permissions = data.get("permissions", {})
        return {
            name: bool(permissions.get(name, {}).get("havePermission"))
            for name in CREATE_PERMISSIONS
        }

    # -- writes ------------------------------------------------------------
    async def create_issue(
        self,
        *,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Task",
        priority: str | None = None,
        labels: list[str] | None = None,
        parent: str | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
            # v3 requires ADF here; a plain string is a 400.
            "description": text_to_adf(description),
        }
        if priority:
            fields["priority"] = {"name": priority}
        if labels:
            # Jira rejects labels containing whitespace.
            fields["labels"] = [l.strip().replace(" ", "-") for l in labels if l.strip()]
        if parent:
            fields["parent"] = {"key": parent}

        created = await self.post_json(
            "/rest/api/3/issue", {"fields": fields}, expected=(201,)
        )
        key = created.get("key")
        return {
            "id": created.get("id"),
            "key": key,
            "url": f"{self.creds.base_url}/browse/{key}" if key else None,
        }

    # -- search ------------------------------------------------------------
    async def search(self, jql: str, *, limit: int = 10) -> list[SimilarTicket]:
        """Run a JQL search.

        Uses the current ``/search/jql`` endpoint, falling back to the legacy
        ``/search`` on deployments that predate it.
        """
        response = await self.request(
            "GET",
            "/rest/api/3/search/jql",
            params={"jql": jql, "maxResults": limit, "fields": SEARCH_FIELDS},
            allow_statuses=(404, 410),
        )
        if response.status_code in (404, 410):
            response = await self.request(
                "GET",
                "/rest/api/3/search",
                params={"jql": jql, "maxResults": limit, "fields": SEARCH_FIELDS},
            )
        issues = response.json().get("issues", [])
        return [self._to_similar(issue) for issue in issues]

    def _to_similar(self, issue: dict[str, Any]) -> SimilarTicket:
        fields = issue.get("fields") or {}
        status = (fields.get("status") or {}).get("name")
        key = issue.get("key")
        return SimilarTicket(
            key=key,
            summary=fields.get("summary"),
            status=status,
            url=f"{self.creds.base_url}/browse/{key}" if key else None,
            source="jira",
        )


def escape_jql(value: str) -> str:
    r"""Escape a user string for embedding in a quoted JQL literal.

    JQL string literals use backslash escaping, so backslashes must be doubled
    before quotes are escaped, or ``\"`` would become ``\\"`` and break out.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')
