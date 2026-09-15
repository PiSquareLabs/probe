import os
import tempfile

import pytest

os.environ.setdefault("PROBE_SECRET_KEY", "unit-test-secret-key")
os.environ.setdefault("PROBE_API_KEY", "")
os.environ.setdefault("PROBE_STATE_PATH", os.path.join(tempfile.gettempdir(), "probe-test.json"))

from app.models import (  # noqa: E402
    ConnectorSettings,
    ElasticCredentials,
    JiraCredentials,
    SetupRequest,
)

ES_URL = "http://es.test:9200"
KIBANA_URL = "http://kibana.test:5601"
JIRA_URL = "https://probe-demo.atlassian.net"


@pytest.fixture
def jira_creds() -> JiraCredentials:
    return JiraCredentials(
        base_url=JIRA_URL,
        account_email="probe-bot@example.com",
        api_token="ATATT3xFfGF0T_pR0b3_dEv_pLaCeHoLdEr_t0k3n_9c1f4e2a",
        project_key="PROBE",
        default_issue_type="Task",
    )


@pytest.fixture
def elastic_creds() -> ElasticCredentials:
    return ElasticCredentials(
        es_url=ES_URL,
        kibana_url=KIBANA_URL,
        api_key="UHJvYmVEZXZLZXlJZDpQcm9iZURldkFwaUtleVNlY3JldFZhbHVl",
        verify_tls=False,
    )


@pytest.fixture
def connector_settings() -> ConnectorSettings:
    return ConnectorSettings(
        connector_id="probe-jira-connector", index_name="search-jira-probe"
    )


@pytest.fixture
def setup_request(jira_creds, elastic_creds, connector_settings) -> SetupRequest:
    return SetupRequest(
        elastic=elastic_creds, jira=jira_creds, connector=connector_settings
    )


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "state.json")
