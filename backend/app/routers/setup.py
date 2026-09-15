"""Setup and connection-management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..clients.elastic import ElasticClient
from ..clients.jira import JiraClient
from ..clients.kibana import KibanaClient
from ..config import Settings, get_settings
from ..deps import get_store, require_api_key
from ..errors import UpstreamError
from ..models import (
    CheckResult,
    ConnectionState,
    ElasticCredentials,
    ElasticValidation,
    JiraCredentials,
    JiraValidation,
    SetupRequest,
)
from ..store import ConnectionStore

router = APIRouter(prefix="/api", tags=["setup"], dependencies=[Depends(require_api_key)])


@router.get("/setup/defaults")
async def setup_defaults(settings: Settings = Depends(get_settings)) -> dict:
    """Seed the setup form from the environment so local dev is one click.

    Secrets are never returned — only whether one is present to pre-fill.
    """
    return {
        "elastic": {
            "es_url": settings.elastic_es_url,
            "kibana_url": settings.kibana_url,
            "verify_tls": settings.elastic_verify_tls,
            "has_api_key": bool(settings.elastic_api_key),
            "username": settings.elastic_username,
        },
        "jira": {
            "base_url": settings.jira_base_url,
            "account_email": settings.jira_account_email,
            "project_key": settings.jira_project_key,
            "default_issue_type": settings.jira_default_issue_type,
            "has_api_token": bool(settings.jira_api_token),
        },
        "connector": {
            "connector_id": settings.es_connector_id,
            "index_name": settings.es_connector_index,
            "name": settings.es_connector_name,
            "sync_interval": settings.es_connector_sync_interval,
        },
        "ticket_route": settings.probe_ticket_route,
    }


@router.get("/connection", response_model=ConnectionState)
async def get_connection(store: ConnectionStore = Depends(get_store)) -> ConnectionState:
    return store.state()


@router.post("/setup", response_model=ConnectionState)
async def save_setup(
    request: SetupRequest, store: ConnectionStore = Depends(get_store)
) -> ConnectionState:
    """Persist credentials. Secrets are encrypted before they touch disk."""
    store.save(request)
    return store.state()


@router.delete("/connection")
async def clear_connection(store: ConnectionStore = Depends(get_store)) -> dict:
    store.clear()
    return {"cleared": True}


@router.post("/validate/jira", response_model=JiraValidation)
async def validate_jira(creds: JiraCredentials) -> JiraValidation:
    """Validate Jira credentials without saving them.

    Checks identity, then the two project permissions that actually gate
    ticket creation. Reported as individual checks so the UI can show exactly
    which one failed.
    """
    checks: list[CheckResult] = []
    client = JiraClient(creds)
    try:
        try:
            me = await client.myself()
        except UpstreamError as exc:
            return JiraValidation(
                ok=False,
                checks=[
                    CheckResult(
                        name="authentication", ok=False, message=exc.message,
                        detail=exc.to_dict(),
                    )
                ],
            )

        checks.append(
            CheckResult(
                name="authentication",
                ok=True,
                message=f"Authenticated as {me.get('displayName')} ({me.get('emailAddress')}).",
            )
        )

        try:
            perms = await client.check_permissions(creds.project_key)
            for name, granted in perms.items():
                checks.append(
                    CheckResult(
                        name=f"permission:{name}",
                        ok=granted,
                        message=(
                            f"{name} granted in {creds.project_key}."
                            if granted
                            else f"{name} is NOT granted in {creds.project_key}. "
                            "Ask a Jira admin to add it to this account's project role."
                        ),
                    )
                )
        except UpstreamError as exc:
            checks.append(
                CheckResult(
                    name="permissions", ok=False, message=exc.message,
                    detail=exc.to_dict(),
                )
            )

        try:
            types = await client.issue_types(creds.project_key)
            names = [t["name"] for t in types]
            checks.append(
                CheckResult(
                    name="issue_types",
                    ok=bool(types),
                    message=(
                        f"{len(types)} issue types available: {', '.join(names[:8])}"
                        if types
                        else f"No creatable issue types found in {creds.project_key}."
                    ),
                    detail={"issue_types": names},
                )
            )
        except UpstreamError as exc:
            checks.append(
                CheckResult(name="issue_types", ok=False, message=exc.message)
            )

        return JiraValidation(
            ok=all(c.ok for c in checks),
            account_id=me.get("accountId"),
            display_name=me.get("displayName"),
            email=me.get("emailAddress"),
            checks=checks,
        )
    finally:
        await client.aclose()


@router.post("/validate/elastic", response_model=ElasticValidation)
async def validate_elastic(
    creds: ElasticCredentials, settings: Settings = Depends(get_settings)
) -> ElasticValidation:
    """Validate Elasticsearch and Kibana without saving.

    Kibana is checked separately because the action connector lives there, and
    a reachable Elasticsearch with an unreachable Kibana is a common and
    confusing half-configured state.
    """
    checks: list[CheckResult] = []
    es = ElasticClient(creds)
    kibana = KibanaClient(creds, api_key=settings.kibana_api_key)
    try:
        try:
            info = await es.info()
        except UpstreamError as exc:
            return ElasticValidation(
                ok=False,
                checks=[
                    CheckResult(
                        name="elasticsearch", ok=False, message=exc.message,
                        detail=exc.to_dict(),
                    )
                ],
            )

        version = (info.get("version") or {}).get("number")
        checks.append(
            CheckResult(
                name="elasticsearch",
                ok=True,
                message=f"Connected to cluster '{info.get('cluster_name')}' ({version}).",
            )
        )

        license_info = await es.license()
        checks.append(
            CheckResult(
                name="license",
                ok=True,
                message=(
                    f"Licence: {license_info.get('type', 'unknown')} "
                    f"({license_info.get('status', 'unknown')})."
                ),
                detail=dict(license_info) if license_info else None,
            )
        )

        try:
            status = await kibana.status()
            checks.append(
                CheckResult(
                    name="kibana",
                    ok=True,
                    message=f"Kibana {status.get('version')} reachable ({status.get('state')}).",
                )
            )
            support = await kibana.connector_type_support()
            checks.append(
                CheckResult(
                    name="jira_action_connector",
                    ok=bool(support.get("enabled_in_license")),
                    message=(
                        "The Jira action connector is available on this licence."
                        if support.get("enabled_in_license")
                        else "The Jira action connector needs a "
                        f"{support.get('minimum_license_required', 'gold')} licence. "
                        "Tickets will be created directly through the Jira API instead."
                    ),
                    detail=support,
                    severity="advisory",
                )
            )
        except UpstreamError as exc:
            checks.append(
                CheckResult(
                    name="kibana",
                    ok=False,
                    message=exc.message,
                    detail=exc.to_dict(),
                )
            )

        # A missing Kibana or an unlicensed action connector is not a failed
        # validation — the app is designed to work without either.
        required = {"elasticsearch"}
        return ElasticValidation(
            ok=all(c.ok for c in checks if c.name in required),
            cluster_name=info.get("cluster_name"),
            version=version,
            license_type=license_info.get("type"),
            license_status=license_info.get("status"),
            checks=checks,
        )
    finally:
        await es.aclose()
        await kibana.aclose()
