"""A fake Elasticsearch + Kibana + Jira, for testing Probe without accounts.

Stands in for all three upstreams on one port so the whole app — setup wizard,
provisioning, ticket creation, duplicate detection — can be exercised end to end
with nothing installed and no Atlassian or Elastic signup.

    uvicorn mock_stack:app --port 9999

By default it reports a **basic** licence, so ticket creation takes the direct
Jira path. To exercise the other path:

    MOCK_LICENSE=gold uvicorn mock_stack:app --port 9999

It keeps state in memory; restart to reset. `GET /__state` shows what it has
received, which is the quickest way to confirm your Jira credentials really
reached the connector configuration.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Probe mock upstream stack")

LICENSE = os.environ.get("MOCK_LICENSE", "basic").lower()
JIRA_ACTION_LICENSED = LICENSE in {"gold", "platinum", "enterprise", "trial"}

STATE: dict = {
    "connector": None,
    "connector_config": None,
    "sync_jobs": 0,
    "action_connector": None,
    "issues": [],
}

# Pre-seeded "already synced" content, so duplicate detection has something to
# find on the very first run.
SEEDED = [
    {
        "key": "PROBE-12",
        "summary": "Checkout returns 500 for guest users on Safari",
        "status": {"name": "In Progress"},
    },
    {
        "key": "PROBE-31",
        "summary": "Nightly ETL job stalls after the connector sync",
        "status": {"name": "To Do"},
    },
]


# ---------------------------------------------------------------- Elasticsearch
@app.get("/")
def cluster_info():
    return {"cluster_name": "probe-mock", "version": {"number": "9.0.1"}}


@app.get("/_license")
def license_info():
    return {"license": {"type": LICENSE, "status": "active"}}


# Declared before /_connector/{connector_id} so the literal path wins the match.
@app.post("/_connector/_sync_job")
def start_sync_job():
    STATE["sync_jobs"] += 1
    return JSONResponse({"id": f"job-{STATE['sync_jobs']}"}, status_code=201)


@app.get("/_connector/_sync_job")
def last_sync_job():
    return {"results": [{"indexed_document_count": len(SEEDED)}]}


@app.get("/_connector/{connector_id}")
def get_connector(connector_id: str):
    if STATE["connector"] is None:
        return JSONResponse({"error": {"reason": "not found"}}, status_code=404)
    return STATE["connector"]


@app.put("/_connector/{connector_id}")
async def put_connector(connector_id: str, request: Request):
    body = await request.json()
    # Pretend the elastic-connectors service is already running and has
    # registered its configuration schema. Set MOCK_CONNECTOR_SERVICE_DOWN=1 to
    # simulate the opposite and see how Probe reports it.
    configuration = (
        {}
        if os.environ.get("MOCK_CONNECTOR_SERVICE_DOWN")
        else {"jira_url": {"value": ""}, "api_token": {"value": ""}}
    )
    STATE["connector"] = {
        "id": connector_id,
        "status": "connected",
        "last_sync_status": None,
        **body,
        "configuration": configuration,
    }
    return {"result": "created"}


@app.put("/_connector/{connector_id}/_configuration")
async def put_configuration(connector_id: str, request: Request):
    STATE["connector_config"] = (await request.json())["values"]
    return {"result": "updated"}


@app.put("/_connector/{connector_id}/_scheduling")
def put_scheduling(connector_id: str):
    return {"result": "updated"}


@app.put("/_connector/{connector_id}/_api_key_id")
def put_api_key_id(connector_id: str):
    return {"result": "updated"}


@app.post("/_security/api_key")
def create_api_key():
    return {"id": "mock-key-id", "encoded": "bW9jay1lbmNvZGVkLWFwaS1rZXk="}


@app.get("/{index}/_count")
def count_documents(index: str):
    return {"count": len(SEEDED)}


@app.post("/{index}/_search")
async def search_index(index: str, request: Request):
    body = await request.json()
    query = _extract_query(body)
    hits = [
        {"_score": _score(query, doc["summary"]), "_source": doc}
        for doc in SEEDED
        if _score(query, doc["summary"]) > 0
    ]
    hits.sort(key=lambda h: h["_score"], reverse=True)
    return {"hits": {"total": {"value": len(hits)}, "hits": hits}}


def _extract_query(body: dict) -> str:
    """Dig the user's text out of whichever query shape Probe sent."""
    query = body.get("query", {})
    if "bool" in query:
        must = query["bool"].get("must") or [{}]
        query = must[0] if isinstance(must, list) else must
    return (query.get("multi_match") or {}).get("query", "")


def _score(query: str, summary: str) -> float:
    """Crude term overlap, scaled to straddle Probe's duplicate threshold."""
    if not query:
        return 0.0
    q = {w for w in query.lower().split() if len(w) > 3}
    s = {w for w in summary.lower().split() if len(w) > 3}
    if not q:
        return 0.0
    return round(len(q & s) / len(q) * 18.0, 2)


# ----------------------------------------------------------------------- Kibana
@app.get("/api/status")
def kibana_status():
    return {
        "name": "probe-mock-kibana",
        "version": {"number": "9.0.1"},
        "status": {"overall": {"level": "available"}},
    }


@app.get("/api/actions/connector_types")
def connector_types():
    return [
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
            "enabled_in_license": JIRA_ACTION_LICENSED,
            "minimum_license_required": "gold",
        },
    ]


@app.get("/api/actions/connectors")
def list_connectors():
    return [STATE["action_connector"]] if STATE["action_connector"] else []


@app.post("/api/actions/connector/{connector_id}")
async def create_action_connector(connector_id: str, request: Request):
    if not JIRA_ACTION_LICENSED:
        return JSONResponse(
            {
                "message": (
                    "Action type .jira is disabled because your basic license "
                    "does not support it."
                )
            },
            status_code=403,
        )
    body = await request.json()
    STATE["action_connector"] = {"id": connector_id, **body}
    return {"id": connector_id, "name": body.get("name")}


@app.post("/api/actions/connector/{connector_id}/_execute")
async def execute_action_connector(connector_id: str, request: Request):
    params = (await request.json()).get("params", {})
    if params.get("subAction") != "pushToService":
        return {"status": "ok", "data": []}
    incident = params.get("subActionParams", {}).get("incident", {})
    STATE["issues"].append({"via": "kibana_connector", "incident": incident})
    number = 100 + len(STATE["issues"])
    return {
        "status": "ok",
        "data": {
            "id": str(10000 + number),
            "title": f"PROBE-{number}",
            "url": f"http://localhost:9999/browse/PROBE-{number}",
        },
    }


# ------------------------------------------------------------------------- Jira
@app.get("/rest/api/3/myself")
def myself():
    return {
        "accountId": "5b10ac8d82e05b22cc7d4ef5",
        "displayName": "Probe Bot",
        "emailAddress": "probe-bot@example.com",
        "active": True,
        "timeZone": "Etc/UTC",
    }


@app.get("/rest/api/3/mypermissions")
def mypermissions():
    # Set MOCK_NO_CREATE_PERMISSION=1 to see how Probe blocks setup when the
    # Jira account cannot create issues.
    granted = not os.environ.get("MOCK_NO_CREATE_PERMISSION")
    return {
        "permissions": {
            "BROWSE_PROJECTS": {"havePermission": True},
            "CREATE_ISSUES": {"havePermission": granted},
        }
    }


@app.get("/rest/api/3/project/search")
def project_search():
    return {
        "values": [
            {"id": "10000", "key": "PROBE", "name": "Probe", "projectTypeKey": "software"},
            {"id": "10001", "key": "OPS", "name": "Operations", "projectTypeKey": "business"},
        ]
    }


@app.get("/rest/api/3/issue/createmeta/{project_key}/issuetypes")
def issue_types(project_key: str):
    return {
        "values": [
            {"id": "10001", "name": "Task", "subtask": False},
            {"id": "10002", "name": "Bug", "subtask": False},
            {"id": "10003", "name": "Story", "subtask": False},
        ]
    }


@app.post("/rest/api/3/issue")
async def create_issue(request: Request):
    body = await request.json()
    STATE["issues"].append({"via": "direct_rest", "fields": body.get("fields")})
    number = 100 + len(STATE["issues"])
    return JSONResponse(
        {"id": str(10000 + number), "key": f"PROBE-{number}"}, status_code=201
    )


@app.get("/rest/api/3/search/jql")
def search_jql():
    return {"issues": []}


@app.get("/browse/{key}")
def browse(key: str):
    return {"note": f"A real Jira would show {key} here."}


# ------------------------------------------------------------------ Inspection
@app.get("/__state")
def inspect_state():
    """What this mock has received — handy for confirming provisioning worked."""
    return {"license": LICENSE, "jira_action_licensed": JIRA_ACTION_LICENSED, **STATE}
