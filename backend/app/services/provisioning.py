"""Provision both Elastic-side integrations from one set of Jira credentials.

This is what the product promises: the operator configures Jira once, in our UI,
and never opens Kibana. The steps are reported individually so a partial success
(content connector created, action connector blocked by licence) is legible
rather than a single opaque failure.
"""

from __future__ import annotations

import logging

from ..clients.elastic import ElasticClient, build_connector_configuration
from ..clients.jira import JiraClient
from ..clients.kibana import KibanaClient
from ..errors import LicenseError, UpstreamError
from ..models import (
    ConnectorSettings,
    JiraCredentials,
    ProvisionRequest,
    ProvisionResult,
    ProvisionStep,
)
from .capabilities import resolve_route

log = logging.getLogger("probe.provisioning")

ACTION_CONNECTOR_ID = "probe-jira-action-connector"
ACTION_CONNECTOR_NAME = "Probe Jira (managed by Probe)"


async def provision(
    *,
    es: ElasticClient,
    kibana: KibanaClient,
    jira_client: JiraClient,
    jira: JiraCredentials,
    settings: ConnectorSettings,
    request: ProvisionRequest,
    preferred_route: str = "auto",
) -> ProvisionResult:
    steps: list[ProvisionStep] = []
    action_connector_id: str | None = None
    connector_api_key: str | None = None
    config_yaml: str | None = None

    # -- 1. Jira must work before anything is provisioned against it --------
    try:
        me = await jira_client.myself()
        perms = await jira_client.check_permissions(jira.project_key)
        missing = [name for name, granted in perms.items() if not granted]
        if missing:
            steps.append(
                ProvisionStep(
                    step="jira_validation",
                    status="failed",
                    message=(
                        f"{me.get('displayName', 'The account')} lacks "
                        f"{', '.join(missing)} in project {jira.project_key}."
                    ),
                    detail={"permissions": perms},
                )
            )
            return ProvisionResult(ok=False, steps=steps)
        steps.append(
            ProvisionStep(
                step="jira_validation",
                status="ok",
                message=(
                    f"Authenticated as {me.get('displayName')} with permission to "
                    f"create issues in {jira.project_key}."
                ),
                detail={"account_id": me.get("accountId"), "permissions": perms},
            )
        )
    except UpstreamError as exc:
        steps.append(
            ProvisionStep(
                step="jira_validation", status="failed", message=exc.message,
                detail=exc.to_dict(),
            )
        )
        return ProvisionResult(ok=False, steps=steps)

    # -- 2. Content connector: Jira -> Elasticsearch -----------------------
    if request.create_content_connector:
        steps.extend(
            await _provision_content_connector(es, jira, settings, request)
        )
        if request.generate_connector_api_key:
            connector_api_key, key_step = await _mint_connector_key(es, settings)
            steps.append(key_step)
        config_yaml = render_connector_config(
            es_url=es.creds.es_url,
            connector_id=settings.connector_id,
            api_key=connector_api_key or "<CONNECTOR_API_KEY>",
        )
    else:
        steps.append(
            ProvisionStep(
                step="content_connector",
                status="skipped",
                message="Content connector creation was not requested.",
            )
        )

    # -- 3. Action connector: Elasticsearch -> Jira (licence permitting) ----
    action_available = False
    if request.create_action_connector:
        support = await kibana.connector_type_support()
        action_available = bool(support.get("enabled_in_license")) and bool(
            support.get("enabled")
        )
        if not action_available:
            steps.append(
                ProvisionStep(
                    step="action_connector",
                    status="skipped",
                    message=(
                        "Skipped: the Jira action connector requires a "
                        f"{support.get('minimum_license_required', 'gold')} licence. "
                        "Tickets will be created directly against the Jira REST API "
                        "instead — no functionality is lost."
                    ),
                    detail=support,
                )
            )
        else:
            try:
                created = await kibana.create_jira_connector(
                    ACTION_CONNECTOR_ID, ACTION_CONNECTOR_NAME, jira
                )
                action_connector_id = created.get("id", ACTION_CONNECTOR_ID)
                steps.append(
                    ProvisionStep(
                        step="action_connector",
                        status="ok",
                        message=(
                            f"Kibana Jira connector '{ACTION_CONNECTOR_NAME}' is ready. "
                            "Elastic will create the tickets."
                        ),
                        detail={"connector_id": action_connector_id},
                    )
                )
            except LicenseError as exc:
                action_available = False
                steps.append(
                    ProvisionStep(
                        step="action_connector",
                        status="skipped",
                        message=exc.message + " Falling back to the direct Jira API.",
                        detail=exc.to_dict(),
                    )
                )
            except UpstreamError as exc:
                action_available = False
                steps.append(
                    ProvisionStep(
                        step="action_connector",
                        status="failed",
                        message=exc.message,
                        detail=exc.to_dict(),
                    )
                )
    else:
        steps.append(
            ProvisionStep(
                step="action_connector",
                status="skipped",
                message="Action connector creation was not requested.",
            )
        )

    ok = not any(step.status == "failed" for step in steps)
    return ProvisionResult(
        ok=ok,
        steps=steps,
        connector_id=settings.connector_id if request.create_content_connector else None,
        index_name=settings.index_name if request.create_content_connector else None,
        action_connector_id=action_connector_id,
        connector_service_api_key=connector_api_key,
        connector_config_yaml=config_yaml,
        effective_route=resolve_route(preferred_route, action_available),
    )


async def _provision_content_connector(
    es: ElasticClient,
    jira: JiraCredentials,
    settings: ConnectorSettings,
    request: ProvisionRequest,
) -> list[ProvisionStep]:
    steps: list[ProvisionStep] = []

    try:
        existing = await es.get_connector(settings.connector_id)
        if existing:
            steps.append(
                ProvisionStep(
                    step="content_connector",
                    status="ok",
                    message=f"Connector '{settings.connector_id}' already exists; reusing it.",
                    detail={"index_name": existing.get("index_name")},
                )
            )
        else:
            await es.create_connector(settings)
            steps.append(
                ProvisionStep(
                    step="content_connector",
                    status="ok",
                    message=(
                        f"Created connector '{settings.connector_id}' syncing into "
                        f"index '{settings.index_name}'."
                    ),
                )
            )
    except UpstreamError as exc:
        steps.append(
            ProvisionStep(
                step="content_connector", status="failed", message=exc.message,
                detail=exc.to_dict(),
            )
        )
        return steps

    # The configuration schema is registered by the connector service, not by
    # Elasticsearch — so check it has checked in before writing values.
    ready = await es.wait_for_configuration(settings.connector_id)
    if not ready:
        steps.append(
            ProvisionStep(
                step="connector_configuration",
                status="failed",
                message=(
                    "The elastic-connectors service has not checked in, so its "
                    "configuration schema is not registered yet. Jira settings "
                    "cannot be written until it is running."
                ),
                detail={
                    "fix": (
                        "Start the connector service (see connectors-config/README) "
                        "and re-run provisioning."
                    )
                },
            )
        )
        return steps

    try:
        await es.update_configuration(
            settings.connector_id, build_connector_configuration(jira, settings)
        )
        steps.append(
            ProvisionStep(
                step="connector_configuration",
                status="ok",
                message=(
                    "Jira credentials pushed into the connector configuration. "
                    "Nothing to configure in Kibana."
                ),
                detail={"projects": settings.projects},
            )
        )
    except UpstreamError as exc:
        steps.append(
            ProvisionStep(
                step="connector_configuration", status="failed", message=exc.message,
                detail=exc.to_dict(),
            )
        )
        return steps

    try:
        await es.update_scheduling(
            settings.connector_id,
            interval=settings.sync_interval,
            enabled=settings.schedule_enabled,
        )
        steps.append(
            ProvisionStep(
                step="connector_scheduling",
                status="ok",
                message=f"Full sync scheduled with cron '{settings.sync_interval}'.",
            )
        )
    except UpstreamError as exc:
        steps.append(
            ProvisionStep(
                step="connector_scheduling", status="failed", message=exc.message,
                detail=exc.to_dict(),
            )
        )

    if request.trigger_sync:
        try:
            await es.start_sync(settings.connector_id)
            steps.append(
                ProvisionStep(
                    step="initial_sync",
                    status="ok",
                    message="Initial full sync queued.",
                )
            )
        except UpstreamError as exc:
            steps.append(
                ProvisionStep(
                    step="initial_sync", status="failed", message=exc.message,
                    detail=exc.to_dict(),
                )
            )

    return steps


async def _mint_connector_key(
    es: ElasticClient, settings: ConnectorSettings
) -> tuple[str | None, ProvisionStep]:
    """Create the API key the connector service uses, and bind it to the connector."""
    try:
        key = await es.create_connector_api_key(
            name=f"probe-connector-{settings.connector_id}",
            index_name=settings.index_name,
        )
        encoded = key.get("encoded")
        try:
            await es.update_api_key_id(settings.connector_id, key.get("id", ""))
        except UpstreamError:
            # Non-fatal: the connector still authenticates with the key itself,
            # this only records the association for the Kibana UI.
            log.warning("Could not record api_key_id on the connector document.")
        return encoded, ProvisionStep(
            step="connector_api_key",
            status="ok",
            message=(
                "API key created for the connector service. Copy it into "
                "connectors-config/config.yml — it is shown only once."
            ),
            detail={"api_key_id": key.get("id")},
        )
    except UpstreamError as exc:
        return None, ProvisionStep(
            step="connector_api_key",
            status="failed",
            message=exc.message,
            detail={
                **exc.to_dict(),
                "fix": (
                    "The Elasticsearch credentials need the manage_api_key and "
                    "manage_connector cluster privileges."
                ),
            },
        )


def render_connector_config(*, es_url: str, connector_id: str, api_key: str) -> str:
    """The config.yml the self-managed elastic-connectors service needs."""
    return (
        f"elasticsearch:\n"
        f"  host: {es_url}\n"
        f"  api_key: {api_key}\n"
        f"\n"
        f"connectors:\n"
        f"  - connector_id: {connector_id}\n"
        f"    service_type: jira\n"
        f"    api_key: {api_key}\n"
    )
