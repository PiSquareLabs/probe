"""Deterministic test for agent_builder.py -- no live Kibana, no Docker,
no network required. Same spirit as smoke_test.py: "does the code do
what it claims" (correct request shape, correct create-vs-update
idempotency, fails soft on a dead connection), not "does the real
Kibana Agent Builder API accept this payload" -- that needs a real
stack (PROBE-PIPELINE-SETUP.md section 3b's kind of live wiring test),
which this machine doesn't have Docker to run.

Fakes urllib.request.urlopen itself (not agent_builder's own functions)
so _request()/_auth_header() -- the actual HTTP-building code that would
talk to a real Kibana -- run for real and get checked, not bypassed.

Run: python test_agent_builder.py
Expect: every line ends "ok", and the last line is
"ALL N CHECKS PASSED".
"""
import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("ES_USERNAME", "elastic")
os.environ.setdefault("ES_PASSWORD", "test-placeholder")
os.environ.pop("KIBANA_API_KEY", None)
os.environ.pop("ES_API_KEY", None)

import agent_builder  # noqa: E402

_checks_passed = 0


def check(name: str, condition: bool) -> None:
    global _checks_passed
    assert condition, f"FAILED: {name}"
    _checks_passed += 1
    print(f"{name}: ok")


class FakeResponse:
    def __init__(self, body: dict | None, status: int = 200):
        self._body = json.dumps(body).encode() if body is not None else b""
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeKibana:
    """Records every request made through urllib and answers as a
    Kibana Agent Builder server would for GET (404 until created),
    POST (create), and PUT (update).
    """

    def __init__(self):
        self.requests: list[urllib.request.Request] = []
        self.tools: dict[str, dict] = {}

    def urlopen(self, req: urllib.request.Request, timeout=None):
        self.requests.append(req)
        path = req.full_url.split("/api/agent_builder/tools", 1)[1] if "/api/agent_builder/tools" in req.full_url else ""
        tool_id = path[1:] if path.startswith("/") else None

        if req.method == "GET":
            if tool_id and tool_id in self.tools:
                return FakeResponse(self.tools[tool_id])
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, io.BytesIO(b'{"message":"not found"}'))

        body = json.loads(req.data.decode())
        if req.method == "POST":
            new_id = body["id"]
            if new_id in self.tools:
                raise urllib.error.HTTPError(req.full_url, 409, "Conflict", None, io.BytesIO(b'{"message":"already exists"}'))
            self.tools[new_id] = body
            return FakeResponse(body)
        if req.method == "PUT":
            if tool_id not in self.tools:
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, io.BytesIO(b'{"message":"not found"}'))
            self.tools[tool_id].update(body)
            return FakeResponse(self.tools[tool_id])
        raise AssertionError(f"unexpected method {req.method}")


class DeadKibana:
    def urlopen(self, req, timeout=None):
        raise urllib.error.URLError("connection refused")


fake = FakeKibana()

with patch("urllib.request.urlopen", side_effect=fake.urlopen):
    # -- auth fallback: no KIBANA_API_KEY -> falls back to es_client's
    # ES_USERNAME/ES_PASSWORD Basic auth, not a crash or an empty header.
    header = agent_builder._auth_header()
    check("auth falls back to Basic when KIBANA_API_KEY unset", header.startswith("Basic "))

    os.environ["KIBANA_API_KEY"] = "test-kibana-key"
    header = agent_builder._auth_header()
    check("KIBANA_API_KEY takes priority when set", header == "ApiKey test-kibana-key")
    del os.environ["KIBANA_API_KEY"]

    # -- get_tool on a tool that doesn't exist yet -> None, not a raise.
    check("get_tool returns None on 404", agent_builder.get_tool("candidate_queries__resource_exhaustion__cart") is None)

    # -- first Jira-Fixed confirmation: tool doesn't exist -> created.
    result = agent_builder.upsert_candidate_query_tool(
        fault_class="resource_exhaustion",
        service="cart",
        root_cause="valkey-cart connection pool exhausted under load",
        steps=["check valkey-cart pool metrics", "bump max connections", "restart cart service"],
        signature={"change_point": "step_change", "metric": "p95_latency", "loudest_service": "cart", "dependency": "valkey-cart"},
    )
    check("first upsert reports created", result == {"provisioned": True, "action": "created", "tool_id": "candidate_queries__resource_exhaustion__cart"})

    create_requests = [r for r in fake.requests if r.method == "POST"]
    check("exactly one POST was made", len(create_requests) == 1)
    payload = json.loads(create_requests[0].data.decode())
    check("created tool id matches Writer._runbook_id convention", payload["id"] == "resource_exhaustion__cart".join(["candidate_queries__", ""]))
    check("created tool type is index_search, not esql", payload["type"] == "index_search")
    check("created tool pattern is the OTel telemetry glob", payload["configuration"] == {"pattern": "*.otel-default"})
    check("description names the service", "cart" in payload["description"])
    check("description carries the verified root cause", "valkey-cart connection pool exhausted" in payload["description"])
    check("description carries the verified steps", "bump max connections" in payload["description"])
    check("description carries the signature", "dependency=valkey-cart" in payload["description"])
    check("tags include the service for discoverability", payload["tags"] == ["probe", "candidate-queries", "cart"])
    check("POST carries the kbn-xsrf header a real Kibana requires", create_requests[0].headers.get("Kbn-xsrf") == "true")

    # -- get_tool now finds it.
    fetched = agent_builder.get_tool("candidate_queries__resource_exhaustion__cart")
    check("get_tool finds the tool after creation", fetched is not None and fetched["id"] == "candidate_queries__resource_exhaustion__cart")

    # -- second Jira-Fixed confirmation on the same (fault_class, service),
    # with a revised root cause -- must update in place, not error on a
    # duplicate id, and must not resend the immutable id/type fields.
    result2 = agent_builder.upsert_candidate_query_tool(
        fault_class="resource_exhaustion",
        service="cart",
        root_cause="valkey-cart eviction policy misconfigured (revised finding)",
        steps=["set maxmemory-policy to allkeys-lru"],
        signature={"change_point": "step_change", "metric": "p95_latency", "loudest_service": "cart", "dependency": "valkey-cart"},
    )
    check("second upsert on the same key reports updated, not created", result2["action"] == "updated")

    update_requests = [r for r in fake.requests if r.method == "PUT"]
    check("exactly one PUT was made", len(update_requests) == 1)
    check("no second POST happened (idempotent, not a duplicate-id error)", len(create_requests) == 1)
    update_payload = json.loads(update_requests[0].data.decode())
    check("PUT omits the immutable id field", "id" not in update_payload)
    check("PUT omits the immutable type field", "type" not in update_payload)
    check("PUT's description reflects the revised root cause", "eviction policy misconfigured" in update_payload["description"])

# -- fail-soft: Writer.mark_jira_fixed must not blow up when Kibana is
# simply not there (no Docker, nothing listening, whatever the reason).
with patch("urllib.request.urlopen", side_effect=DeadKibana().urlopen):
    dead_result = agent_builder.upsert_candidate_query_tool(
        fault_class="queue_backpressure", service="kafka", root_cause="x", steps=[], signature={}
    )
    check("dead Kibana connection reports provisioned=False instead of raising", dead_result["provisioned"] is False)
    check("failure result still names the tool id that would have been created", dead_result["tool_id"] == "candidate_queries__queue_backpressure__kafka")

print(f"\nALL {_checks_passed} CHECKS PASSED")
