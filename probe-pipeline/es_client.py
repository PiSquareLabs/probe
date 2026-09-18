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
import contextvars
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pipeline_log as plog

# Which pipeline stage is issuing the next query -- set by whichever
# stage-level code (Remediator/Correlator) wraps its own query calls in
# `with es_client.stage("remediator"):`, so esql()/search() below can
# attribute the query they log to the right place in the dashboard's
# step-by-step view without every call site having to pass it explicitly.
_CURRENT_STAGE: contextvars.ContextVar[str] = contextvars.ContextVar("es_client_stage", default="unknown")


@contextmanager
def stage(name: str):
    token = _CURRENT_STAGE.set(name)
    try:
        yield
    finally:
        _CURRENT_STAGE.reset(token)


def set_stage(name: str) -> None:
    """Non-context-manager form: set and leave set, for call sites (like
    Remediator.remediate()/Correlator.correlate()'s own top-level methods)
    where wrapping the whole method body in `with stage(...):` would mean
    re-indenting a large existing function. Each stage's own entry point
    sets it once at the top; the next stage's entry point sets it again
    before its own queries run, so there's no cross-stage leakage in the
    single-threaded, one-incident-at-a-time flow every caller here uses."""
    _CURRENT_STAGE.set(name)


def _log_query(kind: str, query_text: str, params: dict | None, elapsed_ms: float,
                row_count: int | None = None, error: str | None = None) -> None:
    # Full query text, not truncated -- this is exactly what the
    # dashboard's "detailed log and executed query" request needs to
    # show, and ES|QL statements here are short enough (a few lines)
    # that there's no real size concern logging them in full.
    plog.emit(_CURRENT_STAGE.get(), "query", query_kind=kind, query=query_text.strip(),
               params=params, elapsed_ms=round(elapsed_ms, 1), row_count=row_count, error=error)

REPO_ROOT = Path(__file__).resolve().parent.parent
START_LOCAL_ENV = REPO_ROOT / "opentelemetry-demo" / "elastic-start-local" / ".env"
ES_URL = os.environ.get("ES_URL", "http://localhost:9200")

# Same field/value mismatch probe-two-tier-detector-v3/change_point.py
# already found and fixed (see that file's own comment): local
# self-hosted Elasticsearch maps OTel span-kind to `span.kind` with
# values like "SERVER"/"CLIENT"; this project's Elastic Cloud
# Serverless project maps it to a field literally called `kind`, with
# Title-case values ("Server", "Client"). correlator.py's own CLIENT-span
# query had the same hardcoded span.kind == "CLIENT" bug, undiscovered
# until reused here -- both now read these same two env vars, plus this
# one for the CLIENT-specific value change_point.py doesn't need.
SPAN_KIND_FIELD = os.environ.get("SPAN_KIND_FIELD", "span.kind")
SPAN_KIND_SERVER_VALUE = os.environ.get("SPAN_KIND_SERVER_VALUE", "SERVER")
SPAN_KIND_CLIENT_VALUE = os.environ.get("SPAN_KIND_CLIENT_VALUE", "CLIENT")


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
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        elapsed = (time.monotonic() - started) * 1000
        error_body = e.read().decode()
        _log_query("esql", query, params, elapsed, error=f"HTTP {e.code}: {error_body[:300]}")
        raise EsqlError(f"ES|QL request failed ({e.code}): {error_body}") from e
    elapsed = (time.monotonic() - started) * 1000
    columns = [c["name"] for c in result["columns"]]
    rows = [dict(zip(columns, row)) for row in result["values"]]
    _log_query("esql", query, params, elapsed, row_count=len(rows))
    return rows


def get_doc(index: str, doc_id: str) -> dict | None:
    """Single doc GET by id -- the fingerprint cache's ~2ms path
    (PROBE-component-contracts.md #7a), not an ES|QL query.
    """
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc/{doc_id}",
        headers={"Authorization": _auth_header()},
        method="GET",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            source = json.loads(resp.read())["_source"]
        _log_query("get_doc", f"GET {index}/_doc/{doc_id}", None, (time.monotonic() - started) * 1000, row_count=1)
        return source
    except urllib.error.HTTPError as e:
        elapsed = (time.monotonic() - started) * 1000
        if e.code == 404:
            _log_query("get_doc", f"GET {index}/_doc/{doc_id}", None, elapsed, row_count=0)
            return None
        error_body = e.read().decode()
        _log_query("get_doc", f"GET {index}/_doc/{doc_id}", None, elapsed, error=f"HTTP {e.code}: {error_body[:300]}")
        raise EsqlError(f"GET {index}/_doc/{doc_id} failed ({e.code}): {error_body}") from e


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


def delete_doc(index: str, doc_id: str) -> None:
    """DELETE by id. Deliberately not used by writer.py (its own
    docstring: "Never: ... deletes a runbook") -- this exists for
    test/dev tooling (techtest_clear.py) that needs to reset a scoped
    (fault_class, service) key between runs, not for the graded write
    path. A 404 (nothing to delete) is not an error here.
    """
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc/{doc_id}",
        headers={"Authorization": _auth_header()},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return
        raise EsqlError(f"DELETE {index}/_doc/{doc_id} failed ({e.code}): {e.read().decode()}") from e


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
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        elapsed = (time.monotonic() - started) * 1000
        error_body = e.read().decode()
        _log_query("search", json.dumps(body), {"index": index}, elapsed, error=f"HTTP {e.code}: {error_body[:300]}")
        raise EsqlError(f"_search request failed ({e.code}): {error_body}") from e
    elapsed = (time.monotonic() - started) * 1000
    hit_count = len(result.get("hits", {}).get("hits", []))
    _log_query("search", json.dumps(body), {"index": index}, elapsed, row_count=hit_count)
    return result
