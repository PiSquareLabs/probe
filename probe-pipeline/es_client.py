"""Parameterized ES|QL client for the Remediator/Correlator's `probe-memory`
tools (PROBE-runbook-search-cases.md #2's `rb_stage0_exact`, `rb_stage1_hybrid`,
`rb_ruled_out`). Separate from probe-detector/es_client.py because those
tools bind `?param` placeholders -- plain string interpolation into ES|QL
is a query-injection risk this module exists specifically to avoid.

Same connection convention as the rest of the repo: local self-hosted
Elasticsearch by default, override via ES_URL / ES_API_KEY / ES_USERNAME
+ ES_PASSWORD for Elastic Cloud/Serverless (see ../RUNNING_ON_ELASTIC_CLOUD.md).
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
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


class EsqlError(RuntimeError):
    pass


def esql(query: str, params: dict | None = None) -> list[dict]:
    """Run a parameterized ES|QL statement, `?name` placeholders bound
    from `params`, and return rows as {column: value} dicts.

    Elasticsearch's ES|QL params are positional in the wire format but
    named in the query text as of the FORK/params syntax used by the
    contract's tools; we bind them by name via the `params` array,
    matching each `?name` occurrence in declaration order.
    """
    body: dict = {"query": query}
    if params:
        body["params"] = [{k: v} for k, v in params.items()]
    req = urllib.request.Request(
        f"{ES_URL}/_query",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": _auth_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise EsqlError(f"ES|QL request failed ({e.code}): {e.read().decode()}") from e
    columns = [c["name"] for c in result["columns"]]
    return [dict(zip(columns, row)) for row in result["values"]]


def get_doc(index: str, doc_id: str) -> dict | None:
    """Single doc GET by id -- the fingerprint cache's ~2ms path
    (PROBE-component-contracts.md #7a), not an ES|QL query.
    """
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc/{doc_id}",
        headers={"Authorization": _auth_header()},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            # `id` isn't a mapped field -- it's the document's _id, a
            # metadata value Elasticsearch never returns inside _source.
            # Every caller (remediator.py, grader.py, writer.py) treats
            # runbook dicts as having an "id" key, so it's injected here,
            # the one place a doc is fetched by id directly.
            return json.loads(resp.read())["_source"] | {"id": doc_id}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise EsqlError(f"GET {index}/_doc/{doc_id} failed ({e.code}): {e.read().decode()}") from e


def index_doc(index: str, doc_id: str, body: dict) -> None:
    """PUT a full document at a specific id -- used by the Writer to
    create a new runbook/ruled-out record. `doc_id` is explicit (not
    auto-generated) so that `(fault_class, service)` maps to exactly
    one runbook id, per contract: never a second document for the same
    key.
    """
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc/{doc_id}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": _auth_header()},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise EsqlError(f"PUT {index}/_doc/{doc_id} failed ({e.code}): {e.read().decode()}") from e


def update_doc(index: str, doc_id: str, partial: dict) -> None:
    """Partial update (merge, not replace) -- used by the Writer for
    occurrences++, status flips, and verified_by appends, so a field
    this call doesn't mention (like a human-edited root_cause) is left
    alone.
    """
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_update/{doc_id}",
        data=json.dumps({"doc": partial}).encode(),
        headers={"Content-Type": "application/json", "Authorization": _auth_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise EsqlError(f"POST {index}/_update/{doc_id} failed ({e.code}): {e.read().decode()}") from e


def search(index: str, body: dict) -> dict:
    """Raw `_search` call -- used for the stage-1 RRF fallback when
    FORK/FUSE isn't available on the target Elasticsearch version
    (PROBE-runbook-search-cases.md #2, tool 2's fallback note).
    """
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_search",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": _auth_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise EsqlError(f"_search request failed ({e.code}): {e.read().decode()}") from e
