"""Work out what this Elastic deployment can actually do, and route accordingly.

The app never assumes a licence tier. It asks Elasticsearch for the licence and
Kibana for `enabled_in_license` on the `.jira` action type, then reports both to
the UI so the operator can see why ticket creation goes the way it does.
"""

from __future__ import annotations

from ..clients.elastic import ElasticClient
from ..clients.kibana import JIRA_ACTION_TYPE_ID, KibanaClient
from ..models import Capability, CapabilityReport


async def build_capability_report(
    es: ElasticClient, kibana: KibanaClient, *, preferred_route: str = "auto"
) -> CapabilityReport:
    license_info = await es.license()
    license_type = license_info.get("type")
    license_status = license_info.get("status")

    jira_action = await kibana.connector_type_support(JIRA_ACTION_TYPE_ID)
    action_available = bool(jira_action.get("enabled_in_license")) and bool(
        jira_action.get("enabled")
    )

    capabilities = [
        Capability(
            id="content_connector",
            label="Elasticsearch Jira content connector (sync tickets into an index)",
            available=True,
            reason=(
                "Self-managed connectors run on a Basic licence. Requires the "
                "elastic-connectors service to be running."
            ),
            required_license="basic",
        ),
        Capability(
            id="action_connector",
            label="Kibana Jira action connector (Elastic creates the ticket)",
            available=action_available,
            reason=str(jira_action.get("reason", "")),
            required_license=str(jira_action.get("minimum_license_required") or "gold"),
        ),
        Capability(
            id="direct_jira",
            label="Direct Jira REST API (this app creates the ticket)",
            available=True,
            reason="Always available — uses the credentials stored in this app.",
            required_license=None,
        ),
        Capability(
            id="document_level_security",
            label="Connector document-level security",
            available=license_type in {"platinum", "enterprise", "trial"},
            reason=(
                "Paid-tier feature. Left disabled so the connector works on Basic."
            ),
            required_license="platinum",
        ),
    ]

    return CapabilityReport(
        license_type=license_type,
        license_status=license_status,
        effective_route=resolve_route(preferred_route, action_available),
        capabilities=capabilities,
    )


def resolve_route(preferred: str, action_connector_available: bool) -> str:
    """Decide which path creates the ticket.

    ``elastic_connector`` is honoured only when the licence actually allows it;
    asking for it on Basic would produce a guaranteed failure at create time,
    so it degrades to the direct path instead.
    """
    if preferred == "direct_jira":
        return "direct_jira"
    if preferred == "elastic_connector":
        return "elastic_connector" if action_connector_available else "direct_jira"
    return "elastic_connector" if action_connector_available else "direct_jira"
