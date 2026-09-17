"""Tier 2 -- the inspector, per PROBE-detector-spec.md §2 and §3.

change_point(service, signal) is a standalone function, not buried in a
scan loop -- the spec is explicit that the Correlator calls it too (for
one extra hop up the dependency graph toward a quiet upstream cause, see
spec §6), so it must work called on its own, given just a service and a
signal name, with no dependency on the Tier 1 scan that usually feeds it.

Ported from ../causal-changepoint-detection/changepoint_detector.py's
change_points_for_service(), narrowed to exactly what the spec asks for:
scoped to one candidate service (the original scanned all services),
2-second buckets over a 5-minute lookback by default (the original used
20-30s buckets over 15-30 minutes -- this is deliberately much tighter,
since Tier 2 only ever runs on a handful of already-shouted candidates,
not all 12 services, so the per-call cost of finer buckets is affordable
here in a way it wasn't for the original's blanket scan).
"""
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START_LOCAL_ENV = REPO_ROOT / "opentelemetry-demo" / "elastic-start-local" / ".env"
ES_URL = os.environ.get("ES_URL", "http://localhost:9200")

DEFAULT_LOOKBACK_MINUTES = 5
DEFAULT_BUCKET_SECONDS = 2  # spec §8: test 1s/2s/5s, pick smallest with a clean p<0.01
SIGNIFICANCE_THRESHOLD = 0.01  # spec uses pvalue < 0.01, stricter than causal-changepoint-detection's 0.05

# spec signal name -> (index pattern, STATS aggregation expression, extra WHERE filter)
_SIGNAL_QUERIES = {
    "p95_latency": (
        "traces-*.otel-default",
        "PERCENTILE(duration, 95)",
        "",
    ),
    "error_rate": (
        "traces-*.otel-default",
        'AVG(CASE(status.code == "Error", 1.0, 0.0))',
        "",
    ),
    "log_error_count": (
        "logs-*.otel-default",
        "COUNT(*)",
        'AND severity_text IN ("ERROR", "FATAL")',
    ),
    "cpu": (
        "metrics-*.otel-default",
        "AVG(COALESCE(TO_DOUBLE(metrics.process.cpu.utilization), "
        "TO_DOUBLE(metrics.jvm.cpu.recent_utilization), "
        "TO_DOUBLE(metrics.process.runtime.cpython.cpu.utilization)))",
        "",
    ),
    "memory": (
        "metrics-*.otel-default",
        "AVG(COALESCE(TO_DOUBLE(metrics.process.memory.usage), "
        "TO_DOUBLE(metrics.jvm.memory.used), TO_DOUBLE(metrics.go.memory.used), "
        "TO_DOUBLE(metrics.process.runtime.cpython.memory), "
        "TO_DOUBLE(metrics.v8js.memory.heap.used)))",
        "",
    ),
}

_SERVICE_FIELD = {
    "traces-*.otel-default": "service.name",
    "logs-*.otel-default": "resource.attributes.service.name",
    "metrics-*.otel-default": "service.name",
}


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


_AUTH_HEADER = _auth_header()


def _esql(query: str) -> dict:
    req = urllib.request.Request(
        f"{ES_URL}/_query",
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json", "Authorization": _AUTH_HEADER},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def change_point(service: str, signal: str,
                  lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
                  bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
                  verbose: bool = False) -> dict | None:
    """Run CHANGE_POINT on one (service, signal) pair. Returns
    {"type", "timestamp", "pvalue"} for the most significant break under
    SIGNIFICANCE_THRESHOLD, or None if there's no significant break (or the
    series was too flat/short for CHANGE_POINT to say anything -- that's a
    legitimate "no break", not an error, so it's swallowed the same way;
    any OTHER query error is still surfaced with --verbose)."""
    if signal not in _SIGNAL_QUERIES:
        raise ValueError(f"unknown signal {signal!r}; one of {list(_SIGNAL_QUERIES)}")
    index, stat_expr, extra_filter = _SIGNAL_QUERIES[signal]
    service_field = _SERVICE_FIELD[index]
    flagd_filter = (
        'AND NOT STARTS_WITH(name, "flagd.evaluation")'
        if index == "traces-*.otel-default" else ""
    )

    query = (
        f'FROM {index} '
        f'| WHERE @timestamp > NOW() - {lookback_minutes} minutes '
        f'AND {service_field} == "{service}" {extra_filter} {flagd_filter} '
        f'| EVAL bucket = DATE_TRUNC({bucket_seconds} seconds, @timestamp) '
        f'| STATS m = {stat_expr} BY bucket '
        f'| SORT bucket ASC '
        f'| CHANGE_POINT m ON bucket '
        f'| WHERE type IS NOT NULL AND pvalue < {SIGNIFICANCE_THRESHOLD}'
    )
    try:
        result = _esql(query)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if "not enough" in body.lower() or "changepoint" in body.lower():
            return None
        if verbose:
            print(f"  [change_point] {service}/{signal} query error: {body[:300]}")
        return None

    cols = [c["name"] for c in result.get("columns", [])]
    rows = [dict(zip(cols, v)) for v in result.get("values", [])]
    if not rows:
        return None
    # keep the most significant (lowest p-value) break if more than one bucket qualifies
    best = min(rows, key=lambda r: r["pvalue"])
    return {"type": best["type"], "timestamp": str(best["bucket"]), "pvalue": best["pvalue"]}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("service")
    ap.add_argument("signal", choices=list(_SIGNAL_QUERIES))
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_MINUTES)
    ap.add_argument("--bucket", type=int, default=DEFAULT_BUCKET_SECONDS)
    args = ap.parse_args()
    print(change_point(args.service, args.signal, args.lookback, args.bucket, verbose=True))
