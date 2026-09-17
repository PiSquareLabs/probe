"""Tier 2 -- the inspector, per PROBE-detector-spec.md §2 and §3.

v3 of this file, fixing change_point() and its caller (detector.py) per
a Tier-2 fix directive (6 changes, no file of that name exists in this
repo -- summarized here, line references are to *this* file after edit):

  1. "not enough data" is no longer silently folded into "no break" --
     it's returned as a distinguishable reason so a tier:1 candidate can
     say *why* it didn't confirm (line ~157).
  2. Resource signals (cpu, memory) get their own, much wider bucket/
     lookback than trace signals -- 2s buckets over 5 minutes on a metric
     exported every 10-60s starves CHANGE_POINT of the ~22 rows it needs
     (line ~60, _SIGNAL_TIMING).
  3. The earliest significant break is reported, not the lowest-p-value
     one -- a 5-minute window spanning both a fault's onset and its
     recovery has two breaks, and the recovery dip often has the lower
     p-value (line ~185).
  4. change_point_batch() lets detector.py fold >3 same-signal candidates
     into one query instead of one call each, using CHANGE_POINT's BY
     partitioning (line ~200).
  5. Exact data stream name instead of a wildcard, SERVER-span filter,
     and a TODO for a not-yet-existing ingest-pipeline boolean (line ~34,
     ~150).
  6. The SORT before CHANGE_POINT is gone -- CHANGE_POINT orders on its
     own ON key, so the prior sort was pure overhead.

change_point(service, signal) remains a standalone function per the
spec's requirement that a future Correlator can call it directly, one
service/signal at a time, without going through Tier 1 at all.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START_LOCAL_ENV = REPO_ROOT / "opentelemetry-demo" / "elastic-start-local" / ".env"
ES_URL = os.environ.get("ES_URL", "http://localhost:9200")

# C5.1: exact data stream, not a wildcard -- narrows the scan to the one
# stream this demo's traces actually land in (no dataset routing here, so
# it's always the "generic" one), configurable for a deployment that does
# route by dataset.
TRACES_DATA_STREAM = os.environ.get("TRACES_DATA_STREAM", "traces-generic.otel-default")

DEFAULT_LOOKBACK_MINUTES = 5
DEFAULT_BUCKET_SECONDS = 2  # spec §8: test 1s/2s/5s, pick smallest with a clean p<0.01
SIGNIFICANCE_THRESHOLD = 0.01  # spec uses pvalue < 0.01, stricter than causal-changepoint-detection's 0.05

# spec signal name -> (index pattern, STATS aggregation expression, extra WHERE filter)
# -- table SHAPE unchanged from v2; only the traces index value changed (C5.1).
_SIGNAL_QUERIES = {
    "p95_latency": (
        TRACES_DATA_STREAM,
        "PERCENTILE(duration, 95)",
        "",
    ),
    "error_rate": (
        TRACES_DATA_STREAM,
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

# C2: per-signal bucket/lookback, kept out of _SIGNAL_QUERIES's structure
# on purpose -- trace signals arrive at request rate and can afford tight
# 2s/5m; cpu/memory arrive at SDK export interval (10-60s) and need a much
# wider window to accumulate the ~22 rows CHANGE_POINT needs; logs sit in
# between. Resource signals still only ever gate Tier 1 -- if they come
# back insufficient_data even at this width, that's left as tier:1, not
# retried wider.
_SIGNAL_TIMING = {
    "p95_latency": (2, 5),
    "error_rate": (2, 5),
    "log_error_count": (10, 10),
    "cpu": (30, 30),
    "memory": (30, 30),
}

RESOURCE_SIGNALS = {"cpu", "memory"}

_SERVICE_FIELD = {
    TRACES_DATA_STREAM: "service.name",
    "logs-*.otel-default": "resource.attributes.service.name",
    "metrics-*.otel-default": "service.name",
}

# C1: counts of insufficient_data outcomes, per (service, signal), for the
# battery-level report described in FIX-tier2-changepoint.md's closing ask
# ("how many Tier 2 failures were insufficient_data vs. genuine no-break").
INSUFFICIENT_DATA_COUNTS: dict[tuple[str, str], int] = defaultdict(int)


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


def _extract_row_count(body: str) -> int | None:
    """Best-effort pull of a row/bucket count out of an ES error message,
    for the insufficient_data reason's `rows` field. Elasticsearch's exact
    wording isn't a stable contract, so this is deliberately forgiving --
    first integer found, or None if the message has none."""
    m = re.search(r"(\d+)", body)
    return int(m.group(1)) if m else None


def _resolve_timing(signal: str, lookback_minutes: int | None,
                     bucket_seconds: int | None) -> tuple[int, int]:
    default_lookback, default_bucket = _SIGNAL_TIMING.get(
        signal, (DEFAULT_LOOKBACK_MINUTES, DEFAULT_BUCKET_SECONDS))
    return (
        lookback_minutes if lookback_minutes is not None else default_lookback,
        bucket_seconds if bucket_seconds is not None else default_bucket,
    )


def _traces_extra_where(index: str) -> str:
    # C5.2: SERVER spans only -- a service's own handling time, cutting
    # the scanned row count 3-5x versus every span kind on that service.
    # C5.3: TODO -- once an ingest pipeline sets `probe.exclude` on flagd's
    # own reconnect spans, replace this STARTS_WITH/LIKE exclusion with
    # `AND probe.exclude != true`. The pipeline doesn't exist yet, so this
    # keeps the old LIKE-equivalent exclusion for now.
    if index != TRACES_DATA_STREAM:
        return ""
    return 'AND span.kind == "SERVER" AND NOT STARTS_WITH(name, "flagd.evaluation")'


def _build_query(index: str, stat_expr: str, extra_filter: str, service_field: str,
                  service_clause: str, lookback_minutes: int, bucket_seconds: int,
                  stats_by: str, change_point_by: str) -> str:
    flagd_filter = _traces_extra_where(index)
    return (
        f'FROM {index} '
        f'| WHERE @timestamp > NOW() - {lookback_minutes} minutes '
        f'AND {service_clause} {extra_filter} {flagd_filter} '
        f'| EVAL bucket = DATE_TRUNC({bucket_seconds} seconds, @timestamp) '
        f'| STATS m = {stat_expr} BY {stats_by} '
        f'| CHANGE_POINT m ON bucket {change_point_by} '  # C6: no SORT -- CHANGE_POINT orders on `bucket` itself
        f'| WHERE type IS NOT NULL AND pvalue < {SIGNIFICANCE_THRESHOLD}'
    )


def _rows_to_result(rows: list[dict]) -> dict | None:
    """C3: earliest significant break wins, not lowest-pvalue -- `rows` is
    already exactly the set with pvalue < SIGNIFICANCE_THRESHOLD (the
    query's own WHERE clause did that filtering), so this just orders by
    bucket and keeps the full ordered list as `breaks` for a caller (e.g.
    a Gate) that cares about a later recovery dip too."""
    if not rows:
        return None
    breaks = sorted(rows, key=lambda r: r["bucket"])
    earliest = breaks[0]
    return {
        "type": earliest["type"],
        "timestamp": str(earliest["bucket"]),
        "pvalue": earliest["pvalue"],
        "breaks": [
            {"type": r["type"], "timestamp": str(r["bucket"]), "pvalue": r["pvalue"]}
            for r in breaks
        ],
    }


def change_point(service: str, signal: str,
                  lookback_minutes: int | None = None,
                  bucket_seconds: int | None = None,
                  verbose: bool = False) -> dict | None:
    """Run CHANGE_POINT on one (service, signal) pair.

    Returns:
      - {"type", "timestamp", "pvalue", "breaks"} for a genuine, confirmed
        break (earliest of possibly several -- C3).
      - {"type": None, "timestamp": None, "pvalue": None,
         "reason": "insufficient_data", "rows": <int|None>} when the
        series was too short/sparse for CHANGE_POINT to run at all (C1) --
        this is now distinguishable from a real "no break".
      - None when the query ran fine and CHANGE_POINT found nothing
        significant -- a genuine no-break, unchanged from v2.

    lookback_minutes/bucket_seconds default to None, which resolves to
    this signal's own timing (_SIGNAL_TIMING, C2) rather than one setting
    forced onto every signal; pass explicit values to override.
    """
    if signal not in _SIGNAL_QUERIES:
        raise ValueError(f"unknown signal {signal!r}; one of {list(_SIGNAL_QUERIES)}")
    index, stat_expr, extra_filter = _SIGNAL_QUERIES[signal]
    service_field = _SERVICE_FIELD[index]
    lookback_minutes, bucket_seconds = _resolve_timing(signal, lookback_minutes, bucket_seconds)

    query = _build_query(
        index, stat_expr, extra_filter, service_field,
        service_clause=f'{service_field} == "{service}"',
        lookback_minutes=lookback_minutes, bucket_seconds=bucket_seconds,
        stats_by="bucket", change_point_by="",
    )
    try:
        result = _esql(query)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if "not enough" in body.lower() or "changepoint" in body.lower():
            INSUFFICIENT_DATA_COUNTS[(service, signal)] += 1
            return {"type": None, "timestamp": None, "pvalue": None,
                    "reason": "insufficient_data", "rows": _extract_row_count(body)}
        if verbose:
            print(f"  [change_point] {service}/{signal} query error: {body[:300]}")
        return None

    cols = [c["name"] for c in result.get("columns", [])]
    rows = [dict(zip(cols, v)) for v in result.get("values", [])]
    return _rows_to_result(rows)


def change_point_batch(services: list[str], signal: str,
                        lookback_minutes: int | None = None,
                        bucket_seconds: int | None = None,
                        verbose: bool = False) -> dict[str, dict | None]:
    """C4: fold >3 same-signal candidates into one query instead of one
    per service, using CHANGE_POINT's BY partitioning to keep each
    service's series separate. Same three-way return shape as
    change_point(), keyed by service. Callers with <=3 candidates on a
    signal should keep calling change_point() per pair -- this is strictly
    for cascades, where Tier 1 shouting 4+ services on the same signal at
    once would otherwise mean 4+ near-identical queries in the same scan.
    """
    if signal not in _SIGNAL_QUERIES:
        raise ValueError(f"unknown signal {signal!r}; one of {list(_SIGNAL_QUERIES)}")
    if not services:
        return {}
    index, stat_expr, extra_filter = _SIGNAL_QUERIES[signal]
    service_field = _SERVICE_FIELD[index]
    lookback_minutes, bucket_seconds = _resolve_timing(signal, lookback_minutes, bucket_seconds)

    shortlist = ", ".join(f'"{s}"' for s in services)
    query = _build_query(
        index, stat_expr, extra_filter, service_field,
        service_clause=f'{service_field} IN ({shortlist})',
        lookback_minutes=lookback_minutes, bucket_seconds=bucket_seconds,
        stats_by=f"bucket, {service_field}", change_point_by=f"BY {service_field}",
    )
    try:
        result = _esql(query)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if "not enough" in body.lower() or "changepoint" in body.lower():
            out = {}
            for s in services:
                INSUFFICIENT_DATA_COUNTS[(s, signal)] += 1
                out[s] = {"type": None, "timestamp": None, "pvalue": None,
                           "reason": "insufficient_data", "rows": _extract_row_count(body)}
            return out
        if verbose:
            print(f"  [change_point_batch] {signal} query error: {body[:300]}")
        return {s: None for s in services}

    cols = [c["name"] for c in result.get("columns", [])]
    rows = [dict(zip(cols, v)) for v in result.get("values", [])]
    by_service = defaultdict(list)
    for row in rows:
        by_service[row[service_field]].append(row)
    return {s: _rows_to_result(by_service.get(s, [])) for s in services}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("service")
    ap.add_argument("signal", choices=list(_SIGNAL_QUERIES))
    ap.add_argument("--lookback", type=int, default=None)
    ap.add_argument("--bucket", type=int, default=None)
    args = ap.parse_args()
    print(change_point(args.service, args.signal, args.lookback, args.bucket, verbose=True))
