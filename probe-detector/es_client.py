"""Thin, stdlib-only ES|QL client. Defaults to the local self-hosted
Elasticsearch (basic auth, credentials read from
../opentelemetry-demo/elastic-start-local/.env), as opposed to
probe-app/detector/src/detector/es_client.py which always targets Elastic
Cloud/Serverless via API key. No `elasticsearch` package dependency --
just urllib, matching the memory-conscious pattern used throughout this
machine's other detector folders (see ../ml-flag-detection/finish_remaining.py
for why: this host is tight on RAM running two demo stacks +
Elasticsearch/Kibana at once, and importing fewer/lighter packages per
process measurably helped avoid OOM kills during long-running collection
scripts).

Override via environment variables to point at Elastic Cloud/Serverless
instead (see ../RUNNING_ON_ELASTIC_CLOUD.md):
    ES_URL       -- e.g. https://my-deployment.es.us-east-1.aws.elastic.cloud
    ES_API_KEY   -- preferred on Cloud/Serverless
    ES_USERNAME / ES_PASSWORD -- basic auth alternative to ES_API_KEY
None of these are required for the default local setup.
"""
import base64
import json
import os
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START_LOCAL_ENV = REPO_ROOT / "opentelemetry-demo" / "elastic-start-local" / ".env"
ES_URL = os.environ.get("ES_URL", "http://localhost:9200")


def _local_password() -> str:
    for line in START_LOCAL_ENV.read_text().splitlines():
        if line.startswith("ES_LOCAL_PASSWORD="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(
        "ES_LOCAL_PASSWORD not found -- set ES_API_KEY, or ES_URL + "
        "ES_USERNAME + ES_PASSWORD, to target a non-local cluster"
    )


def _auth_header() -> str:
    api_key = os.environ.get("ES_API_KEY")
    if api_key:
        return f"ApiKey {api_key}"
    username = os.environ.get("ES_USERNAME", "elastic")
    password = os.environ.get("ES_PASSWORD") or _local_password()
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


_AUTH_HEADER = _auth_header()


def esql_rows(query: str) -> list[dict]:
    """Run an ES|QL query and return rows as a list of {column: value} dicts."""
    req = urllib.request.Request(
        f"{ES_URL}/_query",
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json", "Authorization": _AUTH_HEADER},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    columns = [c["name"] for c in result["columns"]]
    return [dict(zip(columns, row)) for row in result["values"]]
