"""End-to-end API tests through the FastAPI app."""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.deps import get_store
from app.main import create_app
from app.crypto import SecretBox
from app.store import ConnectionStore

from .conftest import ES_URL, JIRA_URL, KIBANA_URL


@pytest.fixture
def client(tmp_path, setup_request):
    store = ConnectionStore(str(tmp_path / "state.json"), SecretBox("test-key"))
    app = create_app()
    app.dependency_overrides[get_store] = lambda: store
    with TestClient(app) as test_client:
        test_client.store = store
        test_client.setup_payload = setup_request.model_dump()
        yield test_client


def test_health_is_open(client):
    assert client.get("/health").json() == {
        "status": "ok",
        "service": "probe-backend",
    }


def test_setup_stores_and_returns_masked_state(client):
    response = client.post("/api/setup", json=client.setup_payload)
    assert response.status_code == 200

    body = response.json()
    assert body["configured"] is True
    assert body["jira"]["project_key"] == "PROBE"
    # The token must never come back out of the API.
    assert client.setup_payload["jira"]["api_token"] not in response.text
    assert "…" in body["jira"]["api_token"]


def test_endpoints_refuse_to_work_before_setup(client):
    response = client.get("/api/connector/status")
    assert response.status_code == 409
    assert response.json()["code"] == "not_configured"


def test_invalid_jira_url_is_rejected_at_the_schema(client):
    payload = client.setup_payload
    payload["jira"]["base_url"] = "probe-demo.atlassian.net"
    assert client.post("/api/setup", json=payload).status_code == 422


def test_project_key_is_normalised_to_uppercase(client):
    payload = client.setup_payload
    payload["jira"]["project_key"] = "probe"
    body = client.post("/api/setup", json=payload).json()
    assert body["jira"]["project_key"] == "PROBE"


@respx.mock
def test_validate_jira_reports_each_check(client):
    respx.get(f"{JIRA_URL}/rest/api/3/myself").mock(
        return_value=httpx.Response(
            200,
            json={
                "accountId": "5b10a",
                "displayName": "Probe Bot",
                "emailAddress": "probe-bot@example.com",
            },
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
    respx.get(f"{JIRA_URL}/rest/api/3/issue/createmeta/PROBE/issuetypes").mock(
        return_value=httpx.Response(
            200, json={"values": [{"id": "1", "name": "Task"}, {"id": "2", "name": "Bug"}]}
        )
    )

    response = client.post("/api/validate/jira", json=client.setup_payload["jira"])
    body = response.json()

    assert body["ok"] is True
    assert body["display_name"] == "Probe Bot"
    names = {c["name"]: c for c in body["checks"]}
    assert names["permission:CREATE_ISSUES"]["ok"] is True
    assert "Task" in names["issue_types"]["message"]


@respx.mock
def test_validate_jira_surfaces_a_missing_permission(client):
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
    respx.get(f"{JIRA_URL}/rest/api/3/issue/createmeta/PROBE/issuetypes").mock(
        return_value=httpx.Response(200, json={"values": []})
    )

    body = client.post("/api/validate/jira", json=client.setup_payload["jira"]).json()
    assert body["ok"] is False
    failed = [c for c in body["checks"] if not c["ok"]]
    assert any("CREATE_ISSUES is NOT granted" in c["message"] for c in failed)


@respx.mock
def test_validate_elastic_reports_the_licence_gate_without_failing(client):
    respx.get(f"{ES_URL}/").mock(
        return_value=httpx.Response(
            200, json={"cluster_name": "probe-dev", "version": {"number": "9.0.1"}}
        )
    )
    respx.get(f"{ES_URL}/_license").mock(
        return_value=httpx.Response(
            200, json={"license": {"type": "basic", "status": "active"}}
        )
    )
    respx.get(f"{KIBANA_URL}/api/status").mock(
        return_value=httpx.Response(
            200,
            json={"name": "kbn", "version": {"number": "9.0.1"}, "status": {"overall": {"level": "available"}}},
        )
    )
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

    body = client.post(
        "/api/validate/elastic", json=client.setup_payload["elastic"]
    ).json()

    # Basic is a perfectly usable configuration, so validation passes...
    assert body["ok"] is True
    assert body["license_type"] == "basic"
    # ...but the gated capability is reported honestly.
    gate = next(c for c in body["checks"] if c["name"] == "jira_action_connector")
    assert gate["ok"] is False
    assert "gold" in gate["message"]


@respx.mock
def test_create_ticket_end_to_end_on_basic(client):
    client.post("/api/setup", json=client.setup_payload)

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
    respx.post(f"{ES_URL}/search-jira-probe/_search").mock(
        return_value=httpx.Response(200, json={"hits": {"total": {"value": 0}, "hits": []}})
    )
    respx.get(f"{JIRA_URL}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": []})
    )
    respx.post(f"{JIRA_URL}/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"id": "9", "key": "PROBE-900"})
    )

    response = client.post(
        "/api/tickets",
        json={"summary": "Connector sync stalls on large attachments", "description": "Repro attached."},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["created"] is True
    assert body["key"] == "PROBE-900"
    assert body["route_used"] == "direct_jira"
    assert body["url"] == f"{JIRA_URL}/browse/PROBE-900"


def test_ticket_summary_is_required(client):
    client.post("/api/setup", json=client.setup_payload)
    assert client.post("/api/tickets", json={"summary": "  "}).status_code == 422


@respx.mock
def test_capabilities_endpoint(client):
    client.post("/api/setup", json=client.setup_payload)
    respx.get(f"{ES_URL}/_license").mock(
        return_value=httpx.Response(
            200, json={"license": {"type": "basic", "status": "active"}}
        )
    )
    respx.get(f"{KIBANA_URL}/api/actions/connector_types").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": ".jira", "enabled": True, "enabled_in_license": False, "minimum_license_required": "gold"}],
        )
    )

    body = client.get("/api/connector/capabilities").json()
    assert body["effective_route"] == "direct_jira"
    assert body["license_type"] == "basic"
    assert len(body["capabilities"]) == 4


def test_api_key_guard_rejects_a_bad_key(tmp_path, setup_request):
    """When PROBE_API_KEY is set, the header is enforced."""
    store = ConnectionStore(str(tmp_path / "s.json"), SecretBox("k"))
    app = create_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: Settings(
        probe_secret_key="k", probe_api_key="expected-key"
    )

    with TestClient(app) as c:
        assert c.get("/api/connection").status_code == 401
        assert (
            c.get("/api/connection", headers={"X-Probe-Key": "wrong"}).status_code == 401
        )
        assert (
            c.get("/api/connection", headers={"X-Probe-Key": "expected-key"}).status_code
            == 200
        )
        # Health stays open so container probes work without the secret.
        assert c.get("/health").status_code == 200


@respx.mock
def test_the_licence_gate_is_advisory_not_a_failure(client):
    """On Basic the gate must not read as something the operator broke."""
    respx.get(f"{ES_URL}/").mock(
        return_value=httpx.Response(
            200, json={"cluster_name": "c", "version": {"number": "9.0.1"}}
        )
    )
    respx.get(f"{ES_URL}/_license").mock(
        return_value=httpx.Response(200, json={"license": {"type": "basic"}})
    )
    respx.get(f"{KIBANA_URL}/api/status").mock(
        return_value=httpx.Response(200, json={"version": {"number": "9.0.1"}})
    )
    respx.get(f"{KIBANA_URL}/api/actions/connector_types").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": ".jira", "enabled": True, "enabled_in_license": False, "minimum_license_required": "gold"}],
        )
    )

    body = client.post("/api/validate/elastic", json=client.setup_payload["elastic"]).json()
    checks = {c["name"]: c for c in body["checks"]}

    assert checks["jira_action_connector"]["severity"] == "advisory"
    assert checks["elasticsearch"]["severity"] == "required"
    assert body["ok"] is True
