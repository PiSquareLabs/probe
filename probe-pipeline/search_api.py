"""Reads from the "SearchAPI Test" endpoint -- an Elasticsearch Search
Application covering GitHub PRs, Jira tickets, and runbooks for one
Hive version, via `correlation_text` (a semantic_text field): a natural-
language query, not a keyword search.

This is the semantic counterpart to github_runbook.py's exact
`(fault_class, service)` filename lookup -- use this when you have a
symptom description instead of a known key, and want related PRs/Jira
tickets pulled in alongside runbooks for broader context.

Same status as github_runbook.py/catalog_runbook.py: read-only,
deliberately off the graded path. remediator.py/correlator.py never
import this -- the graded retrieval loop only ever reads `probe-memory`
(contract #7a); this is for a human, a chat tool, or a display layer.

Requires SEARCH_API_KEY in the environment. The key currently in use is
a full-cluster-access key, not scoped to just these three indices --
never write it into a file or commit it (the source doc's own warning).

One real limitation of this endpoint, not something this module can
work around: it only exposes `params.query`, no index filter and no
size-per-source control. Results are capped at 10 total, ranked by
semantic score across all three sources mixed together -- so
search_runbooks() can legitimately return fewer than `size` (even zero)
if PRs/Jira happen to dominate the global top 10 for a given query.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

SEARCH_APP_URL = (
    "https://probe-c38fec.es.ap-south-1.aws.elastic-cloud.com"
    "/_application/search_application/hive-359167f6-3dbd-4cb3-b167-90df3fd4fb63/_search"
)

RUNBOOKS_INDEX = "content-probe-runbooks-lq2pe5"
GITHUB_PR_INDEX = "content-probe-gh"
JIRA_INDEX = "content-probe-jira"


class SearchApiError(RuntimeError):
    pass


def _auth_header() -> str:
    api_key = os.environ.get("SEARCH_API_KEY")
    if not api_key:
        raise SearchApiError("SEARCH_API_KEY not set")
    return f"ApiKey {api_key}"


def search(query: str) -> list[dict]:
    """Raw semantic search across all three sources in one call, up to
    10 results total, ranked by semantic similarity to `query` --
    genuinely relevant results typically score 0.65-0.85 per the source
    doc. Each result: {"index", "score", "id", "source"}.
    """
    body = {"params": {"query": query}}
    req = urllib.request.Request(
        SEARCH_APP_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": _auth_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SearchApiError(f"search request failed ({e.code}): {e.read().decode()}") from e
    hits = result.get("hits", {}).get("hits", [])
    return [
        {"index": h["_index"], "score": h["_score"], "id": h["_id"], "source": h.get("_source", {})}
        for h in hits
    ]


def search_runbooks(query: str) -> list[dict]:
    """Just the runbook hits from search(query) -- see this module's
    docstring for why this can legitimately come back empty even when
    a relevant runbook exists (it just didn't rank in the global top 10).
    """
    return [h for h in search(query) if h["index"] == RUNBOOKS_INDEX]


def search_prs(query: str) -> list[dict]:
    return [h for h in search(query) if h["index"] == GITHUB_PR_INDEX]


def search_jira(query: str) -> list[dict]:
    return [h for h in search(query) if h["index"] == JIRA_INDEX]
