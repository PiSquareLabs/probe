"""
Derive the real service call graph empirically from trace data, instead of
hardcoding it from memory of the demo's architecture (which drifts across
forks/versions) or relying on ES|QL LOOKUP JOIN (fragile on a high-volume
traces index — the right side must be lookup-mode, and it chokes on scale).

Approach: pull recent spans (span_id, parent_span_id, service.name), build
an in-memory span_id -> service map, then for every span whose parent_span_id
resolves to a *different* service, that's a caller -> callee edge. Counting
these across many requests gives real, weighted dependency edges.

This also naturally excludes flagd's own streaming spans from the graph as
long as they don't call across services (they don't — EventStream just
reconnects within the same service).

Usage:
    python derive_service_graph.py --minutes 30 --out service_graph.json
"""
import argparse
import base64
import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START_LOCAL_ENV = REPO_ROOT / "opentelemetry-demo" / "elastic-start-local" / ".env"
ES_URL = "http://localhost:9200"


def es_password():
    for line in START_LOCAL_ENV.read_text().splitlines():
        if line.startswith("ES_LOCAL_PASSWORD="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("ES_LOCAL_PASSWORD not found")


AUTH_HEADER = "Basic " + base64.b64encode(f"elastic:{es_password()}".encode()).decode()


def es_post(path, body):
    req = urllib.request.Request(
        f"{ES_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": AUTH_HEADER},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def fetch_spans(minutes: int, page_size: int = 5000, max_pages: int = 40):
    """Page through recent spans via search_after, keeping only the fields
    needed to reconstruct parent -> child service edges."""
    spans = []
    search_after = None
    body_base = {
        "size": page_size,
        "_source": ["span_id", "parent_span_id", "resource.attributes.service.name"],
        "query": {"bool": {"filter": [
            {"range": {"@timestamp": {"gte": f"now-{minutes}m"}}},
            {"exists": {"field": "parent_span_id"}},
        ]}},
        "sort": [{"@timestamp": "asc"}, {"_shard_doc": "asc"}],
    }
    for _ in range(max_pages):
        body = dict(body_base)
        if search_after:
            body["search_after"] = search_after
        result = es_post("/traces-*,.ds-traces-*/_search", body)
        hits = result.get("hits", {}).get("hits", [])
        if not hits:
            break
        spans.extend(hits)
        search_after = hits[-1]["sort"]
        if len(hits) < page_size:
            break
    return spans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--out", default="service_graph.json")
    args = ap.parse_args()

    print(f"Fetching spans from the last {args.minutes} minutes...", flush=True)
    hits = fetch_spans(args.minutes)
    print(f"Fetched {len(hits)} spans", flush=True)

    span_to_service = {}
    span_to_parent = {}
    for h in hits:
        src = h["_source"]
        span_id = src.get("span_id")
        parent_id = src.get("parent_span_id")
        service = src.get("resource", {}).get("attributes", {}).get("service.name")
        if not span_id or not service:
            continue
        span_to_service[span_id] = service
        if parent_id:
            span_to_parent[span_id] = parent_id

    edges = Counter()
    unresolved = 0
    for span_id, parent_id in span_to_parent.items():
        child_service = span_to_service.get(span_id)
        parent_service = span_to_service.get(parent_id)
        if parent_service is None:
            unresolved += 1  # parent span outside our fetched window/page
            continue
        if parent_service != child_service:
            edges[(parent_service, child_service)] += 1

    print(f"{len(edges)} distinct caller->callee edges, "
          f"{unresolved} spans with an out-of-window parent", flush=True)

    graph = {
        "edges": [
            {"caller": c, "callee": cc, "count": n}
            for (c, cc), n in sorted(edges.items(), key=lambda kv: -kv[1])
        ],
    }
    Path(args.out).write_text(json.dumps(graph, indent=2))
    print(f"Wrote {len(graph['edges'])} edges to {args.out}")
    for e in graph["edges"]:
        print(f"  {e['caller']:20s} -> {e['callee']:20s}  ({e['count']} calls)")


if __name__ == "__main__":
    main()
