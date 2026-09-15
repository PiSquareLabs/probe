"""The licence gate is the crux of the design, so it gets the most coverage.

The Kibana `.jira` action connector is Gold+. On Basic the app must detect that
and route ticket creation to the direct Jira API instead of failing.
"""

import httpx
import pytest
import respx

from app.clients.elastic import ElasticClient
from app.clients.jira import JiraClient
from app.clients.kibana import KibanaClient
from app.errors import LicenseError
from app.models import TicketRequest
from app.services.capabilities import build_capability_report, resolve_route
from app.services.tickets import create_ticket

from .conftest import ES_URL, JIRA_URL, KIBANA_URL

BASIC_TYPES = [
    {
        "id": ".index",
        "name": "Index",
        "enabled": True,
        "enabled_in_license": True,
        "minimum_license_required": "basic",
    },
    {
        "id": ".jira",
        "name": "Jira",
        "enabled": True,
        "enabled_in_license": False,
        "minimum_license_required": "gold",
    },
]

GOLD_TYPES = [
    {
        "id": ".jira",
        "name": "Jira",
        "enabled": True,
        "enabled_in_license": True,
        "minimum_license_required": "gold",
    }
]


def mock_connector_types(types):
    return respx.get(f"{KIBANA_URL}/api/actions/connector_types").mock(
        return_value=httpx.Response(200, json=types)
    )


def mock_license(kind="basic"):
    return respx.get(f"{ES_URL}/_license").mock(
        return_value=httpx.Response(
            200, json={"license": {"type": kind, "status": "active"}}
        )
    )


# -- route resolution ------------------------------------------------------
@pytest.mark.parametrize(
    "preferred,available,expected",
    [
        ("auto", True, "elastic_connector"),
        ("auto", False, "direct_jira"),
        ("direct_jira", True, "direct_jira"),
        ("direct_jira", False, "direct_jira"),
        ("elastic_connector", True, "elastic_connector"),
        # Asking for the connector on Basic would be a guaranteed failure at
        # create time, so it degrades instead.
        ("elastic_connector", False, "direct_jira"),
    ],
)
def test_route_resolution(preferred, available, expected):
    assert resolve_route(preferred, available) == expected


# -- licence probing -------------------------------------------------------
@respx.mock
async def test_basic_license_reports_the_connector_as_unavailable(elastic_creds):
    mock_connector_types(BASIC_TYPES)
    async with KibanaClient(elastic_creds) as kibana:
        support = await kibana.connector_type_support()

    assert support["supported"] is True
    assert support["enabled_in_license"] is False
    assert support["minimum_license_required"] == "gold"


@respx.mock
async def test_unreachable_kibana_degrades_rather_than_raising(elastic_creds):
    respx.get(f"{KIBANA_URL}/api/actions/connector_types").mock(
        side_effect=httpx.ConnectError("refused")
    )
    async with KibanaClient(elastic_creds) as kibana:
        support = await kibana.connector_type_support()

    assert support["enabled_in_license"] is False
    assert "could not be reached" in support["reason"]


@respx.mock
async def test_create_connector_on_basic_raises_license_error(elastic_creds, jira_creds):
    respx.post(f"{KIBANA_URL}/api/actions/connector/probe-jira-action-connector").mock(
        return_value=httpx.Response(
            403, json={"message": "Action type .jira is disabled because your basic license does not support it."}
        )
    )
    async with KibanaClient(elastic_creds) as kibana:
        with pytest.raises(LicenseError) as exc:
            await kibana.create_jira_connector(
                "probe-jira-action-connector", "Probe Jira", jira_creds
            )

    assert exc.value.required == "gold"
    assert "direct Jira REST API" in (exc.value.hint or "")


# -- capability report -----------------------------------------------------
@respx.mock
async def test_capability_report_on_basic(elastic_creds):
    mock_license("basic")
    mock_connector_types(BASIC_TYPES)

    async with ElasticClient(elastic_creds) as es, KibanaClient(elastic_creds) as kibana:
        report = await build_capability_report(es, kibana)

    assert report.license_type == "basic"
    assert report.effective_route == "direct_jira"

    caps = {c.id: c for c in report.capabilities}
    assert caps["action_connector"].available is False
    assert caps["action_connector"].required_license == "gold"
    # The two things that must still work on a free tier:
    assert caps["content_connector"].available is True
    assert caps["direct_jira"].available is True
    assert caps["document_level_security"].available is False


@respx.mock
async def test_capability_report_on_gold(elastic_creds):
    mock_license("gold")
    mock_connector_types(GOLD_TYPES)

    async with ElasticClient(elastic_creds) as es, KibanaClient(elastic_creds) as kibana:
        report = await build_capability_report(es, kibana)

    assert report.effective_route == "elastic_connector"
    assert {c.id: c.available for c in report.capabilities}["action_connector"] is True


# -- end-to-end ticket routing --------------------------------------------
@respx.mock
async def test_direct_route_creates_through_the_jira_api(
    jira_creds, elastic_creds, connector_settings
):
    respx.post(f"{ES_URL}/search-jira-probe/_search").mock(
        return_value=httpx.Response(404)
    )
    respx.get(f"{JIRA_URL}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": []})
    )
    create = respx.post(f"{JIRA_URL}/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"id": "1", "key": "PROBE-101"})
    )

    async with ElasticClient(elastic_creds) as es, JiraClient(jira_creds) as jira, KibanaClient(
        elastic_creds
    ) as kibana:
        result = await create_ticket(
            request=TicketRequest(summary="Payment webhook retries forever"),
            jira=jira_creds,
            connector=connector_settings,
            es=es,
            jira_client=jira,
            kibana=kibana,
            route="direct_jira",
        )

    assert result.created is True
    assert result.key == "PROBE-101"
    assert result.route_used == "direct_jira"
    assert create.called


@respx.mock
async def test_connector_route_creates_through_kibana(
    jira_creds, elastic_creds, connector_settings
):
    respx.post(f"{ES_URL}/search-jira-probe/_search").mock(
        return_value=httpx.Response(404)
    )
    respx.get(f"{JIRA_URL}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": []})
    )
    execute = respx.post(
        f"{KIBANA_URL}/api/actions/connector/probe-jira-action-connector/_execute"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "id": "10099",
                    "title": "PROBE-202",
                    "url": f"{JIRA_URL}/browse/PROBE-202",
                },
            },
        )
    )
    direct = respx.post(f"{JIRA_URL}/rest/api/3/issue")

    async with ElasticClient(elastic_creds) as es, JiraClient(jira_creds) as jira, KibanaClient(
        elastic_creds
    ) as kibana:
        result = await create_ticket(
            request=TicketRequest(summary="Nightly ETL job stalls"),
            jira=jira_creds,
            connector=connector_settings,
            es=es,
            jira_client=jira,
            kibana=kibana,
            route="elastic_connector",
        )

    assert result.key == "PROBE-202"
    assert result.route_used == "elastic_connector"
    assert execute.called
    assert not direct.called


@respx.mock
async def test_a_failing_kibana_connector_falls_back_to_jira(
    jira_creds, elastic_creds, connector_settings
):
    """A rotated secret or a restarting Kibana must not lose the ticket."""
    respx.post(f"{ES_URL}/search-jira-probe/_search").mock(
        return_value=httpx.Response(404)
    )
    respx.get(f"{JIRA_URL}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": []})
    )
    respx.post(
        f"{KIBANA_URL}/api/actions/connector/probe-jira-action-connector/_execute"
    ).mock(
        return_value=httpx.Response(
            200, json={"status": "error", "message": "Unauthorized: invalid apiToken"}
        )
    )
    direct = respx.post(f"{JIRA_URL}/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"id": "2", "key": "PROBE-303"})
    )

    async with ElasticClient(elastic_creds) as es, JiraClient(jira_creds) as jira, KibanaClient(
        elastic_creds
    ) as kibana:
        result = await create_ticket(
            request=TicketRequest(summary="Search latency spike"),
            jira=jira_creds,
            connector=connector_settings,
            es=es,
            jira_client=jira,
            kibana=kibana,
            route="elastic_connector",
        )

    assert result.key == "PROBE-303"
    assert result.route_used == "direct_jira"
    assert direct.called


@respx.mock
async def test_duplicates_come_from_elasticsearch_when_the_index_has_data(
    jira_creds, elastic_creds, connector_settings
):
    respx.post(f"{ES_URL}/search-jira-probe/_search").mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": {
                    "total": {"value": 1},
                    "hits": [
                        {
                            "_score": 12.5,
                            "_source": {
                                "key": "PROBE-9",
                                "summary": "Search latency spike on the catalog index",
                                "status": {"name": "In Progress"},
                            },
                        },
                        # Below the duplicate threshold: related, not a duplicate.
                        {
                            "_score": 2.1,
                            "_source": {"key": "PROBE-3", "summary": "Unrelated"},
                        },
                    ],
                }
            },
        )
    )
    respx.post(f"{JIRA_URL}/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"id": "3", "key": "PROBE-404"})
    )

    async with ElasticClient(elastic_creds) as es, JiraClient(jira_creds) as jira, KibanaClient(
        elastic_creds
    ) as kibana:
        result = await create_ticket(
            request=TicketRequest(summary="Search latency spike"),
            jira=jira_creds,
            connector=connector_settings,
            es=es,
            jira_client=jira,
            kibana=kibana,
            route="direct_jira",
        )

    assert [d.key for d in result.duplicates] == ["PROBE-9"]
    assert result.duplicates[0].source == "elasticsearch"
    assert result.duplicates[0].status == "In Progress"


@respx.mock
async def test_elasticsearch_hits_get_a_clickable_jira_url(
    jira_creds, elastic_creds, connector_settings
):
    """The connector does not always index a url, so one is derived from the key."""
    respx.post(f"{ES_URL}/search-jira-probe/_search").mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": {
                    "total": {"value": 1},
                    "hits": [
                        {"_score": 20.0, "_source": {"key": "PROBE-9", "summary": "x"}}
                    ],
                }
            },
        )
    )
    respx.post(f"{JIRA_URL}/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"id": "3", "key": "PROBE-500"})
    )

    async with ElasticClient(elastic_creds) as es, JiraClient(jira_creds) as jira, KibanaClient(
        elastic_creds
    ) as kibana:
        result = await create_ticket(
            request=TicketRequest(summary="anything at all"),
            jira=jira_creds,
            connector=connector_settings,
            es=es,
            jira_client=jira,
            kibana=kibana,
            route="direct_jira",
        )

    assert result.duplicates[0].url == f"{JIRA_URL}/browse/PROBE-9"
