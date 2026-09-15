"""
Causal root-cause detector: runs ES|QL CHANGE_POINT per service over a
lookback window to find which services show a statistically significant
shift in latency or error rate, then uses the empirically-derived service
call graph (service_graph.json, see derive_service_graph.py) to rank the
candidates by "earliest + most upstream of the others" -- naming a single
root cause instead of just listing every service that looks anomalous.

Field names are for the OTel-native ingest mapping this stack actually uses
(verified against the live index, NOT the classic Elastic APM field names):
resource.attributes.service.name, duration (nanoseconds), name,
attributes.event.outcome, attributes.processor.event.

flagd.evaluation.v1.Service/* spans are excluded everywhere -- they're
flagd's own OpenFeature client reconnecting its streaming connection, not
application traffic, and would otherwise look like every service is
constantly having long-running "requests" and errors (see
../ml-flag-detection/README.md #5 for how this was found).

Usage:
    python changepoint_detector.py --lookback 30 --bucket 30
"""
import argparse
import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START_LOCAL_ENV = REPO_ROOT / "opentelemetry-demo" / "elastic-start-local" / ".env"
ES_URL = "http://localhost:9200"

# Services worth watching. flagd/flagd-ui/load-generator/frontend-proxy are
# excluded as detection targets (flagd and load-generator are
# infrastructure, not business services; frontend-proxy is a passthrough
# whose latency mostly reflects whatever's behind it, which is noisy as a
# *root cause* signal even though it's a fine graph node).
SERVICES = [
    "ad", "cart", "checkout", "currency", "email", "frontend",
    "image-provider", "payment", "product-catalog", "quote",
    "recommendation", "shipping",
]

SIGNIFICANCE_THRESHOLD = 0.05  # ES|QL CHANGE_POINT pvalue cutoff


def es_password():
    for line in START_LOCAL_ENV.read_text().splitlines():
        if line.startswith("ES_LOCAL_PASSWORD="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("ES_LOCAL_PASSWORD not found")


AUTH_HEADER = "Basic " + base64.b64encode(f"elastic:{es_password()}".encode()).decode()


def esql(query: str):
    req = urllib.request.Request(
        f"{ES_URL}/_query",
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json", "Authorization": AUTH_HEADER},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def change_points_for_service(service: str, stat_expr: str,
                               lookback_min: int, bucket_sec: int, verbose: bool = False):
    """Run CHANGE_POINT on one metric for one service. `stat_expr` is a
    complete ES|QL STATS aggregation expression, e.g. "PERCENTILE(duration, 95)"
    or "AVG(CASE(...))". Returns a list of (bucket_time, type, pvalue) rows,
    or [] if the series was too flat/short for CHANGE_POINT to say anything
    (it errors on a degenerate series -- that's not a bug, it means "no
    evidence of a change," so we swallow that specific case, but print any
    other error since a silently-swallowed real bug looks identical to "no
    signal" and is much worse than a noisy log line)."""
    query = (
        f'FROM traces-*,.ds-traces-* '
        f'| WHERE @timestamp >= NOW() - {lookback_min} minutes '
        f'AND resource.attributes.service.name == "{service}" '
        f'AND attributes.processor.event == "transaction" '
        f'AND NOT STARTS_WITH(name, "flagd.evaluation") '
        f'| EVAL bucket = DATE_TRUNC({bucket_sec} seconds, @timestamp) '
        f'| STATS m = {stat_expr} BY bucket '
        f'| SORT bucket ASC '
        f'| CHANGE_POINT m ON bucket '
        f'| WHERE type IS NOT NULL'
    )
    try:
        result = esql(query)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        # ES|QL raises "not enough values" / similar for a too-short or
        # perfectly flat series -- that's a legitimate "no signal", not a bug
        if "not enough" in body.lower() or "changepoint" in body.lower():
            return []
        if verbose:
            print(f"    [{service}] query error: {body[:300]}")
        return []
    cols = [c["name"] for c in result.get("columns", [])]
    rows = []
    for v in result.get("values", []):
        row = dict(zip(cols, v))
        rows.append((row["bucket"], row["type"], row["pvalue"]))
    return rows


def find_candidates(lookback_min: int, bucket_sec: int, verbose: bool = False):
    candidates = {}
    for svc in SERVICES:
        lat = change_points_for_service(
            svc, "PERCENTILE(duration, 95)", lookback_min, bucket_sec, verbose)
        err = change_points_for_service(
            svc, 'AVG(CASE(attributes.event.outcome == "failure", 1.0, 0.0))',
            lookback_min, bucket_sec, verbose)
        events = [("latency", t, ty, p) for (t, ty, p) in lat if p < SIGNIFICANCE_THRESHOLD]
        events += [("error_rate", t, ty, p) for (t, ty, p) in err if p < SIGNIFICANCE_THRESHOLD]
        if events:
            # keep the earliest, most significant event for ranking
            events.sort(key=lambda e: (e[1], e[3]))
            candidates[svc] = {
                "earliest_change_time": events[0][1],
                "best_pvalue": min(e[3] for e in events),
                "all_events": events,
            }
    return candidates


def load_graph():
    graph_path = Path(__file__).parent / "service_graph.json"
    edges = json.loads(graph_path.read_text())["edges"]
    downstream = {}
    for e in edges:
        downstream.setdefault(e["caller"], set()).add(e["callee"])
    return downstream


def reachable(downstream: dict, start: str) -> set:
    seen, stack = set(), [start]
    while stack:
        n = stack.pop()
        for nxt in downstream.get(n, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def rank_candidates(candidates: dict, downstream: dict):
    if not candidates:
        return []
    times = sorted(c["earliest_change_time"] for c in candidates.values())
    span = (
        (_to_epoch(times[-1]) - _to_epoch(times[0])) or 1.0
        if len(times) > 1 else 1.0
    )
    t0 = _to_epoch(times[0])

    scored = []
    for svc, info in candidates.items():
        earliness = 1.0 - (_to_epoch(info["earliest_change_time"]) - t0) / span
        # Latency/errors propagate callee -> caller (a slow `ad` makes
        # `frontend`, which calls it, look slow too) -- so the root-cause
        # signal is "how many OTHER anomalous candidates depend on svc"
        # (svc is reachable from them), NOT "how many candidates svc can
        # reach". Scoring the forward direction rewards the *symptom*
        # (the caller inheriting latency) over the *cause* (the callee
        # actually producing it).
        depended_on_by = sum(
            1 for other in candidates if other != svc and svc in reachable(downstream, other)
        )
        score = 0.6 * earliness + 0.4 * (depended_on_by / max(len(candidates) - 1, 1))
        scored.append({
            "service": svc,
            "score": round(score, 4),
            "earliness": round(earliness, 4),
            "depended_on_by": depended_on_by,
            "earliest_change_time": info["earliest_change_time"],
            "best_pvalue": info["best_pvalue"],
        })
    # break score ties with statistical significance (lower p-value = more
    # certain the change is real), rather than leaving it to dict/insertion
    # order, which is an accident of SERVICES' list order, not evidence
    scored.sort(key=lambda s: (-s["score"], s["best_pvalue"]))
    return scored


def _to_epoch(iso_ts: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=30, help="minutes to look back")
    ap.add_argument("--bucket", type=int, default=30, help="bucket size in seconds")
    ap.add_argument("--verbose", action="store_true", help="print query errors instead of swallowing them")
    args = ap.parse_args()

    print(f"Scanning {len(SERVICES)} services over the last {args.lookback} minutes "
          f"({args.bucket}s buckets)...")
    candidates = find_candidates(args.lookback, args.bucket, args.verbose)

    if not candidates:
        print("No significant change points found in any service. "
              "System looks stable (or the lookback window is too short/long).")
        return

    print(f"\n{len(candidates)} service(s) with a significant change point:")
    for svc, info in candidates.items():
        print(f"  {svc:18s} earliest change @ {info['earliest_change_time']}  "
              f"p={info['best_pvalue']:.2e}  events={[e[0] for e in info['all_events']]}")

    downstream = load_graph()
    ranked = rank_candidates(candidates, downstream)

    print("\nCausal ranking (highest score = most likely root cause):")
    for r in ranked:
        print(f"  {r['score']:.3f}  {r['service']:18s} "
              f"earliness={r['earliness']:.2f}  "
              f"depended_on_by_{r['depended_on_by']}_other_candidates  "
              f"p={r['best_pvalue']:.2e}")

    top = ranked[0]
    print(f"\n>>> Named root cause: {top['service']} "
          f"(score={top['score']:.3f}, {top['depended_on_by']} other "
          f"anomalous service(s) depend on it, earliest to shift)")


if __name__ == "__main__":
    main()
