#!/usr/bin/env python3
"""
PROBE — build probe-service-graph + probe-service-reachability from REAL traces.

Why this exists as a script and not as ES|QL: deriving caller->callee edges needs a
self-join of traces on parent_span_id == span_id. ES|QL's LOOKUP JOIN requires the
right-hand side to be an `index.mode: lookup` index, and `traces-*.otel-*` is logsdb
[research/01-VERIFIED-FACTS.md F3/F5]. So the join happens here, once.

That is defensible and worth saying out loud: service TOPOLOGY changes on deploy
timescales, not per-second, so it is materialised on a schedule. The per-incident
reasoning stays in ES|QL.

  ES_URL=... ES_AUTH=user:pass python3 build_service_graph.py [--minutes 30]
  --seed   skip Elasticsearch, write the known Astronomy Shop topology instead
           (use ONLY if trace derivation fails; say so on stage)
"""
import argparse, base64, json, os, sys, urllib.request, itertools
from collections import defaultdict

ES_URL = os.environ.get("ES_URL", "http://localhost:9200").rstrip("/")

# Fallback only. Derived from opentelemetry-demo/.env service addresses.
SEED_EDGES = [
    ("frontend-proxy","frontend"),("load-generator","frontend-proxy"),
    ("frontend","ad"),("frontend","cart"),("frontend","checkout"),
    ("frontend","currency"),("frontend","product-catalog"),
    ("frontend","recommendation"),("frontend","image-provider"),
    ("checkout","cart"),("checkout","currency"),("checkout","email"),
    ("checkout","payment"),("checkout","product-catalog"),
    ("checkout","shipping"),("checkout","kafka"),
    ("recommendation","product-catalog"),("shipping","quote"),
    ("cart","valkey-cart"),("kafka","accounting"),("kafka","fraud-detection"),
]

def _req(path, payload=None, method="GET"):
    url = f"{ES_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if os.environ.get("ES_AUTH_HEADER"):
        r.add_header("Authorization", os.environ["ES_AUTH_HEADER"])
    elif os.environ.get("ES_AUTH"):
        r.add_header("Authorization", "Basic " +
                     base64.b64encode(os.environ["ES_AUTH"].encode()).decode())
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read())

def esql(query):
    return _req("/_query", {"query": query}, "POST")

def derive_edges(minutes):
    """Pull (span_id, parent_span_id, service) and join in memory."""
    rows, page, size = [], 0, 10000
    q = (f'FROM traces-*.otel-* | WHERE @timestamp >= NOW() - {minutes} minutes '
         f'| EVAL svc = resource.attributes.service.name '
         f'| KEEP span_id, parent_span_id, svc | LIMIT {size}')
    res = esql(q)
    cols = [c["name"] for c in res["columns"]]
    for v in res["values"]:
        rows.append(dict(zip(cols, v)))
    print(f"  fetched {len(rows)} spans", file=sys.stderr)

    svc_of = {r["span_id"]: r["svc"] for r in rows if r.get("span_id")}
    edges = defaultdict(int)
    unresolved = 0
    for r in rows:
        p = r.get("parent_span_id")
        if not p:
            continue
        parent_svc = svc_of.get(p)
        if parent_svc is None:
            unresolved += 1      # parent span outside the fetched page
            continue
        if parent_svc != r["svc"]:
            edges[(parent_svc, r["svc"])] += 1
    print(f"  {len(edges)} distinct edges, {unresolved} spans whose parent "
          f"was outside the page", file=sys.stderr)
    return edges

def closure(edges):
    """Transitive closure with hop counts. ~15 nodes, so BFS per node is fine."""
    adj = defaultdict(set)
    for (a, b) in edges:
        adj[a].add(b)
    nodes = set(adj) | {b for (_, b) in edges}
    adj = dict(adj)                    # freeze: defaultdict grows during lookup
    out = []
    for start in nodes:
        seen, frontier, hops = {start}, {start}, 0
        while frontier and hops < 10:
            hops += 1
            nxt = set()
            for n in frontier:
                nxt |= (adj.get(n, set()) - seen)
            for n in nxt:
                out.append((start, n, hops))
            seen |= nxt
            frontier = nxt
    return out

def bulk(lines, label):
    if not lines:
        print(f"  !! nothing to index for {label}", file=sys.stderr); return
    body = "".join(lines).encode()
    r = urllib.request.Request(f"{ES_URL}/_bulk", data=body, method="POST")
    r.add_header("Content-Type", "application/x-ndjson")
    if os.environ.get("ES_AUTH_HEADER"):
        r.add_header("Authorization", os.environ["ES_AUTH_HEADER"])
    elif os.environ.get("ES_AUTH"):
        r.add_header("Authorization", "Basic " +
                     base64.b64encode(os.environ["ES_AUTH"].encode()).decode())
    with urllib.request.urlopen(r, timeout=120) as resp:
        res = json.loads(resp.read())
    print(f"  indexed {label}: errors={res.get('errors')}", file=sys.stderr)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.seed:
        print("SEED MODE — known topology, NOT derived from traces. Say so on stage.",
              file=sys.stderr)
        edges = {e: 0 for e in SEED_EDGES}
    else:
        edges = derive_edges(a.minutes)
        if not edges:
            print("!! no edges derived — falling back to --seed", file=sys.stderr)
            edges = {e: 0 for e in SEED_EDGES}

    reach = closure(edges)
    print(f"  closure: {len(reach)} (ancestor, descendant) pairs", file=sys.stderr)

    if a.dry_run:
        for (c, e), n in sorted(edges.items()):
            print(f"  {c} -> {e}  ({n})")
        return

    gl = []
    for (c, e), n in edges.items():
        gl.append(json.dumps({"index": {"_index": "probe-service-graph",
                                        "_id": f"{c}->{e}"}}) + "\n")
        gl.append(json.dumps({"caller": c, "callee": e, "calls": n}) + "\n")
    bulk(gl, "probe-service-graph")

    rl = []
    for (anc, desc, hops) in reach:
        rl.append(json.dumps({"index": {"_index": "probe-service-reachability",
                                        "_id": f"{anc}=>{desc}"}}) + "\n")
        rl.append(json.dumps({"ancestor": anc, "descendant": desc,
                              "hops": hops}) + "\n")
    bulk(rl, "probe-service-reachability")

if __name__ == "__main__":
    main()
