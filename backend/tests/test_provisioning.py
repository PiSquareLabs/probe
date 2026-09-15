"""Provisioning must report per-step outcomes, not one opaque success/failure."""

import httpx
import respx

from app.clients.elastic import ElasticClient, build_connector_configuration
from app.clients.jira import JiraClient
from app.clients.kibana import KibanaClient
from app.models import ProvisionRequest
from app.services.provisioning import provision, render_connector_config

from .conftest import ES_URL, JIRA_URL, KIBANA_URL

CONNECTOR_ID = "probe-jira-connector"


def mock_jira_ok():
    respx.get(f"{JIRA_URL}/rest/api/3/myself").mock(
        return_value=httpx.Response(
            200, json={"accountId": "5b10a", "displayName": "Probe Bot"}
        )
    )
    respx.get(f"{JIRA_URL}/rest/api/3/mypermissions").mock(
        return_value=httpx.Response(
            200,
            json={
                "permissions": {
                    "BROWSE_PROJECTS": {"havePermission": True},
                    "CREATE_ISSUES": {"havePermission": True},
                }
            },
        )
    )


def mock_basic_license_kibana():
    respx.get(f"{KIBANA_URL}/api/actions/connector_types").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": ".jira",
                    "enabled": True,
                    "enabled_in_license": False,
                    "minimum_license_required": "gold",
                }
            ],
        )
    )


async def _no_schema(self, connector_id, **kwargs):
    """Stand-in for the poll loop: the connector service never checks in."""
    return None


def steps_by_name(result):
    return {s.step: s for s in result.steps}


# -- configuration mapping -------------------------------------------------
def test_ui_credentials_map_onto_connector_fields(jira_creds, connector_settings):
    config = build_connector_configuration(jira_creds, connector_settings)

    assert config["data_source"] == "jira_cloud"
    assert config["jira_url"] == jira_creds.base_url
    assert config["account_email"] == jira_creds.account_email
    assert config["api_token"] == jira_creds.api_token
    assert config["projects"] == "*"
    # Paid-tier features stay off so the connector runs on Basic.
    assert config["use_document_level_security"] is False


def test_rendered_config_yaml_is_what_the_service_expects():
    yaml = render_connector_config(
        es_url="http://localhost:9200", connector_id="c1", api_key="KEY"
    )
    assert "elasticsearch:" in yaml
    assert "  host: http://localhost:9200" in yaml
    assert "  - connector_id: c1" in yaml
    assert "    service_type: jira" in yaml


# -- happy path ------------------------------------------------------------
@respx.mock
async def test_full_provision_on_basic_skips_only_the_action_connector(
    jira_creds, elastic_creds, connector_settings
):
    mock_jira_ok()
    mock_basic_license_kibana()

    respx.get(f"{ES_URL}/_connector/{CONNECTOR_ID}").mock(
        side_effect=[
            httpx.Response(404),  # does not exist yet
            httpx.Response(  # after creation, the service has registered a schema
                200,
                json={
                    "id": CONNECTOR_ID,
                    "configuration": {"jira_url": {"value": ""}},
                },
            ),
        ]
    )
    create = respx.put(f"{ES_URL}/_connector/{CONNECTOR_ID}").mock(
        return_value=httpx.Response(201, json={"result": "created"})
    )
    config = respx.put(f"{ES_URL}/_connector/{CONNECTOR_ID}/_configuration").mock(
        return_value=httpx.Response(200, json={"result": "updated"})
    )
    respx.put(f"{ES_URL}/_connector/{CONNECTOR_ID}/_scheduling").mock(
        return_value=httpx.Response(200, json={"result": "updated"})
    )
    respx.put(f"{ES_URL}/_connector/{CONNECTOR_ID}/_api_key_id").mock(
        return_value=httpx.Response(200, json={"result": "updated"})
    )
    respx.post(f"{ES_URL}/_connector/_sync_job").mock(
        return_value=httpx.Response(201, json={"id": "job-1"})
    )
    respx.post(f"{ES_URL}/_security/api_key").mock(
        return_value=httpx.Response(
            200, json={"id": "key-id", "encoded": "ZW5jb2RlZC1rZXk="}
        )
    )

    async with ElasticClient(elastic_creds) as es, KibanaClient(
        elastic_creds
    ) as kibana, JiraClient(jira_creds) as jira:
        result = await provision(
            es=es,
            kibana=kibana,
            jira_client=jira,
            jira=jira_creds,
            settings=connector_settings,
            request=ProvisionRequest(),
        )

    steps = steps_by_name(result)
    assert result.ok is True  # a skipped step is not a failure
    assert steps["jira_validation"].status == "ok"
    assert steps["content_connector"].status == "ok"
    assert steps["connector_configuration"].status == "ok"
    assert steps["connector_scheduling"].status == "ok"
    assert steps["initial_sync"].status == "ok"
    assert steps["connector_api_key"].status == "ok"

    assert steps["action_connector"].status == "skipped"
    assert "gold" in steps["action_connector"].message
    assert "no functionality is lost" in steps["action_connector"].message

    assert result.effective_route == "direct_jira"
    assert result.connector_service_api_key == "ZW5jb2RlZC1rZXk="
    assert "ZW5jb2RlZC1rZXk=" in result.connector_config_yaml
    assert create.called and config.called

    # The Jira token reached the connector configuration, which is the point.
    import json

    sent = json.loads(config.calls[0].request.read())["values"]
    assert sent["api_token"] == jira_creds.api_token
    assert sent["jira_url"] == jira_creds.base_url


# -- failure paths ---------------------------------------------------------
@respx.mock
async def test_missing_create_permission_stops_before_touching_elastic(
    jira_creds, elastic_creds, connector_settings
):
    respx.get(f"{JIRA_URL}/rest/api/3/myself").mock(
        return_value=httpx.Response(200, json={"displayName": "Probe Bot"})
    )
    respx.get(f"{JIRA_URL}/rest/api/3/mypermissions").mock(
        return_value=httpx.Response(
            200,
            json={
                "permissions": {
                    "BROWSE_PROJECTS": {"havePermission": True},
                    "CREATE_ISSUES": {"havePermission": False},
                }
            },
        )
    )
    es_create = respx.put(f"{ES_URL}/_connector/{CONNECTOR_ID}")

    async with ElasticClient(elastic_creds) as es, KibanaClient(
        elastic_creds
    ) as kibana, JiraClient(jira_creds) as jira:
        result = await provision(
            es=es,
            kibana=kibana,
            jira_client=jira,
            jira=jira_creds,
            settings=connector_settings,
            request=ProvisionRequest(),
        )

    assert result.ok is False
    assert len(result.steps) == 1
    assert "CREATE_ISSUES" in result.steps[0].message
    assert not es_create.called


@respx.mock
async def test_connector_service_not_running_is_reported_clearly(
    jira_creds, elastic_creds, connector_settings, monkeypatch
):
    """Writing configuration before the service checks in silently no-ops.

    That is the single most confusing failure in this integration, so it gets
    an explicit message rather than a mysteriously empty connector.
    """
    mock_jira_ok()
    mock_basic_license_kibana()
    # Do not spend the real back-off waiting for a service we know is absent.
    monkeypatch.setattr(ElasticClient, "wait_for_configuration", _no_schema)

    # Never returns a configuration schema: the service has not checked in.
    respx.get(f"{ES_URL}/_connector/{CONNECTOR_ID}").mock(
        return_value=httpx.Response(200, json={"id": CONNECTOR_ID, "configuration": {}})
    )
    config = respx.put(f"{ES_URL}/_connector/{CONNECTOR_ID}/_configuration")
    respx.post(f"{ES_URL}/_security/api_key").mock(
        return_value=httpx.Response(200, json={"id": "k", "encoded": "e"})
    )
    respx.put(f"{ES_URL}/_connector/{CONNECTOR_ID}/_api_key_id").mock(
        return_value=httpx.Response(200, json={})
    )

    async with ElasticClient(elastic_creds) as es, KibanaClient(
        elastic_creds
    ) as kibana, JiraClient(jira_creds) as jira:
        result = await provision(
            es=es,
            kibana=kibana,
            jira_client=jira,
            jira=jira_creds,
            settings=connector_settings,
            request=ProvisionRequest(),
        )

    step = steps_by_name(result)["connector_configuration"]
    assert result.ok is False
    assert step.status == "failed"
    assert "has not checked in" in step.message
    # Crucially, it did not pretend to write the configuration.
    assert not config.called


@respx.mock
async def test_gold_license_creates_the_action_connector(
    jira_creds, elastic_creds, connector_settings
):
    mock_jira_ok()
    respx.get(f"{KIBANA_URL}/api/actions/connector_types").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": ".jira",
                    "enabled": True,
                    "enabled_in_license": True,
                    "minimum_license_required": "gold",
                }
            ],
        )
    )
    created = respx.post(
        f"{KIBANA_URL}/api/actions/connector/probe-jira-action-connector"
    ).mock(
        return_value=httpx.Response(
            200, json={"id": "probe-jira-action-connector", "name": "Probe Jira"}
        )
    )

    async with ElasticClient(elastic_creds) as es, KibanaClient(
        elastic_creds
    ) as kibana, JiraClient(jira_creds) as jira:
        result = await provision(
            es=es,
            kibana=kibana,
            jira_client=jira,
            jira=jira_creds,
            settings=connector_settings,
            request=ProvisionRequest(create_content_connector=False),
        )

    assert steps_by_name(result)["action_connector"].status == "ok"
    assert result.action_connector_id == "probe-jira-action-connector"
    assert result.effective_route == "elastic_connector"

    import json

    body = json.loads(created.calls[0].request.read())
    assert body["connector_type_id"] == ".jira"
    assert body["config"]["apiUrl"] == jira_creds.base_url
    assert body["config"]["projectKey"] == "PROBE"
    assert body["secrets"]["apiToken"] == jira_creds.api_token
    # Kibana rejects non-GET requests without this header.
    assert created.calls[0].request.headers["kbn-xsrf"] == "true"
