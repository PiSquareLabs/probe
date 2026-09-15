"""Request and response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

TicketRoute = Literal["auto", "elastic_connector", "direct_jira"]


def _strip_trailing_slash(value: str) -> str:
    return value.rstrip("/")


# --------------------------------------------------------------------------
# Connection settings
# --------------------------------------------------------------------------
class JiraCredentials(BaseModel):
    base_url: str = Field(..., examples=["https://probe-demo.atlassian.net"])
    account_email: str = Field(..., examples=["probe-bot@example.com"])
    api_token: str = Field(..., min_length=8)
    project_key: str = Field(..., examples=["PROBE"])
    default_issue_type: str = "Task"

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = _strip_trailing_slash(v.strip())
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v

    @field_validator("project_key")
    @classmethod
    def _key(cls, v: str) -> str:
        return v.strip().upper()


class ElasticCredentials(BaseModel):
    es_url: str = Field(..., examples=["http://localhost:9200"])
    kibana_url: str = Field(..., examples=["http://localhost:5601"])
    api_key: str = ""
    username: str = ""
    password: str = ""
    verify_tls: bool = True

    @field_validator("es_url", "kibana_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = _strip_trailing_slash(v.strip())
        if not v.startswith(("http://", "https://")):
            raise ValueError("URLs must start with http:// or https://")
        return v


class ConnectorSettings(BaseModel):
    connector_id: str = "probe-jira-connector"
    index_name: str = "search-jira-probe"
    name: str = "Probe Jira content connector"
    projects: str = "*"
    sync_interval: str = "0 0 */3 * * ?"
    schedule_enabled: bool = True
    ssl_enabled: bool = False
    retry_count: int = 3
    concurrent_downloads: int = 50

    @field_validator("index_name")
    @classmethod
    def _index(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("index_name is required")
        return v


class SetupRequest(BaseModel):
    elastic: ElasticCredentials
    jira: JiraCredentials
    connector: ConnectorSettings = ConnectorSettings()
    ticket_route: TicketRoute = "auto"


class MaskedJira(BaseModel):
    base_url: str
    account_email: str
    api_token: str
    project_key: str
    default_issue_type: str


class MaskedElastic(BaseModel):
    es_url: str
    kibana_url: str
    api_key: str
    username: str
    password: str
    verify_tls: bool


class ConnectionState(BaseModel):
    configured: bool
    elastic: MaskedElastic | None = None
    jira: MaskedJira | None = None
    connector: ConnectorSettings | None = None
    ticket_route: TicketRoute = "auto"
    updated_at: str | None = None


# --------------------------------------------------------------------------
# Validation / capability reporting
# --------------------------------------------------------------------------
class CheckResult(BaseModel):
    name: str
    ok: bool
    message: str
    detail: dict[str, Any] | None = None
    # "required" blocks progress; "advisory" is informational — a licence gate
    # the app is designed to work around should not read as an error.
    severity: Literal["required", "advisory"] = "required"


class JiraValidation(BaseModel):
    ok: bool
    account_id: str | None = None
    display_name: str | None = None
    email: str | None = None
    checks: list[CheckResult] = []


class ElasticValidation(BaseModel):
    ok: bool
    cluster_name: str | None = None
    version: str | None = None
    license_type: str | None = None
    license_status: str | None = None
    checks: list[CheckResult] = []


class Capability(BaseModel):
    id: str
    label: str
    available: bool
    reason: str
    required_license: str | None = None


class CapabilityReport(BaseModel):
    license_type: str | None = None
    license_status: str | None = None
    effective_route: Literal["elastic_connector", "direct_jira"]
    capabilities: list[Capability]


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------
class ProvisionRequest(BaseModel):
    create_content_connector: bool = True
    create_action_connector: bool = True
    trigger_sync: bool = True
    generate_connector_api_key: bool = True


class ProvisionStep(BaseModel):
    step: str
    status: Literal["ok", "skipped", "failed"]
    message: str
    detail: dict[str, Any] | None = None


class ProvisionResult(BaseModel):
    ok: bool
    steps: list[ProvisionStep]
    connector_id: str | None = None
    index_name: str | None = None
    action_connector_id: str | None = None
    connector_service_api_key: str | None = None
    connector_config_yaml: str | None = None
    effective_route: Literal["elastic_connector", "direct_jira"] | None = None


class SyncStatus(BaseModel):
    connector_id: str
    index_name: str | None = None
    status: str | None = None
    service_type: str | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None
    last_synced: str | None = None
    last_seen: str | None = None
    configured: bool = False
    docs_indexed: int | None = None
    document_count: int | None = None


# --------------------------------------------------------------------------
# Tickets
# --------------------------------------------------------------------------
class TicketRequest(BaseModel):
    summary: str = Field(..., min_length=3, max_length=255)
    description: str = ""
    issue_type: str | None = None
    priority: str | None = None
    labels: list[str] = []
    project_key: str | None = None
    parent: str | None = None
    route: TicketRoute | None = None
    check_duplicates: bool = True

    @field_validator("summary")
    @classmethod
    def _summary(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("summary is required")
        return v


class SimilarTicket(BaseModel):
    key: str | None = None
    summary: str | None = None
    status: str | None = None
    url: str | None = None
    score: float | None = None
    source: Literal["elasticsearch", "jira"] = "elasticsearch"


class TicketResponse(BaseModel):
    created: bool
    key: str | None = None
    id: str | None = None
    url: str | None = None
    route_used: Literal["elastic_connector", "direct_jira"] | None = None
    duplicates: list[SimilarTicket] = []
    message: str


class SearchRequest(BaseModel):
    query: str = ""
    size: int = Field(10, ge=1, le=50)
    project_key: str | None = None


class SearchResponse(BaseModel):
    source: Literal["elasticsearch", "jira"]
    total: int
    results: list[SimilarTicket]
    note: str | None = None
