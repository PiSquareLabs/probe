"""Kibana Agent Builder client -- provisions the per-service "candidate
query" tool the Writer creates on a Jira Fixed confirmation
(PROBE-component-contracts.md #6, "On Jira Fixed -> verified_by += human,
status -> verified").

A human closing a Jira ticket as Fixed is the one signal in this system
that a runbook's (fault_class, service) diagnosis was checked by a person,
not just self-confirmed by the confirm agent (PROBE-runbook-search-cases.md
#2). That's the moment it's worth spending a one-time write to give that
service its own Agent Builder tool, scoped to its own telemetry, so the
next incident on this service doesn't fall back to a generic full-cluster
tool to draft ad-hoc queries.

This is an `index_search` tool, not an `esql` tool: the whole point is
that the agent drafts its own query for whatever the next incident on
this service looks like, seeded by this runbook's known signature and
steps in the tool description (Agent Builder picks tools, and shapes
queries with index_search tools, from the description alone). A fixed
ES|QL tool would just be a second copy of rb_stage0_exact -- this repo
already has that (PROBE-runbook-search-cases.md #2, tool 1).

Off the graded path, same as probe_memory_lookup and github_runbook.py:
nothing in remediator.py or correlator.py calls this. Only Writer.mark_jira_fixed
does, and only after a human has already verified the runbook.

Same fails-soft convention as writer.py's default_open_pr_stub: no Kibana
client wired (KIBANA_URL unset/unreachable) reports rather than raises,
so a missing Agent Builder integration doesn't take down the probe-memory
write it's paired with.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import es_client

KIBANA_URL = os.environ.get("KIBANA_URL", "http://localhost:5601")

# OTel-shaped telemetry only -- excludes probe-memory/probe-runs/probe-changes,
# which are this pipeline's own bookkeeping indices, not service telemetry.
# The narrowest single glob that still covers traces + logs + metrics
# (probe-two-tier-detector-v3/change_point.py's three data streams); a
# per-signal-type pattern would mean three tools per service, against the
# "minimize toolsets" guidance every Agent Builder tool's description costs
# agent context on every turn.
TELEMETRY_PATTERN = "*.otel-default"


class AgentBuilderError(RuntimeError):
    pass


def _auth_header() -> str:
    api_key = os.environ.get("KIBANA_API_KEY")
    if api_key:
        return f"ApiKey {api_key}"
    # Blank KIBANA_API_KEY falls back to the same Elasticsearch credentials
    # es_client.py uses -- same convention backend/.env.example documents
    # for the FastAPI side (ELASTIC_API_KEY, then ELASTIC_USERNAME/PASSWORD).
    return es_client._auth_header()


def _request(method: str, path: str, body: dict | None = None) -> dict | None:
    headers = {"Authorization": _auth_header(), "kbn-xsrf": "true"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{KIBANA_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if method == "GET" and e.code == 404:
            return None
        raise AgentBuilderError(f"{method} {path} failed ({e.code}): {e.read().decode()}") from e


def get_tool(tool_id: str) -> dict | None:
    return _request("GET", f"/api/agent_builder/tools/{tool_id}")


def create_tool(payload: dict) -> dict:
    return _request("POST", "/api/agent_builder/tools", payload)


def update_tool(tool_id: str, payload: dict) -> dict:
    # PUT only accepts description/configuration/tags -- id and type are
    # immutable, so callers must not include them (kibana-agent-builder
    # skill, step 4's API constraints).
    return _request("PUT", f"/api/agent_builder/tools/{tool_id}", payload)


def _tool_id(fault_class: str, service: str) -> str:
    # Mirrors writer.py's Writer._runbook_id() so the tool id is traceable
    # straight back to the runbook that earned it.
    return f"candidate_queries__{fault_class}__{service}"


def _description(fault_class: str, service: str, root_cause: str, steps: list[str], signature: dict) -> str:
    steps_text = "; ".join(steps) if steps else "(none recorded)"
    signature_text = ", ".join(f"{k}={v}" for k, v in signature.items() if v is not None)
    return (
        f"Drafts candidate Elasticsearch queries over {service}'s own OTel telemetry "
        f"(traces, logs, metrics) for incidents that look like this service's verified "
        f"{fault_class} runbook. Use this instead of a full-cluster search tool when the "
        f"incident's loudest or dependency service is '{service}'.\n"
        f"Known signature: {signature_text or '(none recorded)'}.\n"
        f"Verified root cause: {root_cause}\n"
        f"Verified steps: {steps_text}\n"
        f"Always filter to this service (service.name, or resource.attributes.service.name "
        f"for logs) before drawing conclusions -- this pattern spans every service's telemetry, "
        f"not just {service}'s."
    )


def upsert_candidate_query_tool(
    fault_class: str, service: str, root_cause: str, steps: list[str], signature: dict
) -> dict:
    """Creates (or refreshes the description on) the per-service candidate-
    query tool. Idempotent -- a second Jira Fixed on the same
    (fault_class, service), or a runbook whose steps changed since, just
    updates the description in place rather than erroring on a duplicate id.
    """
    tool_id = _tool_id(fault_class, service)
    configuration = {"pattern": TELEMETRY_PATTERN}
    description = _description(fault_class, service, root_cause, steps, signature)
    tags = ["probe", "candidate-queries", service]

    try:
        existing = get_tool(tool_id)
        if existing is None:
            create_tool({"id": tool_id, "type": "index_search", "description": description, "configuration": configuration, "tags": tags})
            return {"provisioned": True, "action": "created", "tool_id": tool_id}
        update_tool(tool_id, {"description": description, "configuration": configuration, "tags": tags})
        return {"provisioned": True, "action": "updated", "tool_id": tool_id}
    except (AgentBuilderError, urllib.error.URLError) as e:
        return {"provisioned": False, "reason": str(e), "tool_id": tool_id}
