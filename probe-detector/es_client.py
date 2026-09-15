"""Thin, stdlib-only ES|QL client for the local self-hosted Elasticsearch
(basic auth), as opposed to probe-app/detector/src/detector/es_client.py
which targets Elastic Cloud/Serverless via API key. No `elasticsearch`
package dependency -- just urllib, matching the memory-conscious pattern
used throughout this machine's other detector folders (see
../ml-flag-detection/finish_remaining.py for why: this host is tight on
RAM running two demo stacks + Elasticsearch/Kibana at once, and importing
fewer/lighter packages per process measurably helped avoid OOM kills
during long-running collection scripts).
"""
import base64
import json
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START_LOCAL_ENV = REPO_ROOT / "opentelemetry-demo" / "elastic-start-local" / ".env"
ES_URL = "http://localhost:9200"


def _es_password() -> str:
    for line in START_LOCAL_ENV.read_text().splitlines():
        if line.startswith("ES_LOCAL_PASSWORD="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("ES_LOCAL_PASSWORD not found")


_AUTH_HEADER = "Basic " + base64.b64encode(f"elastic:{_es_password()}".encode()).decode()


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
