"""Tier 1 — the lookout, per PROBE-detector-spec.md §2.

Z-score scan ported from ../probe-detector/anomaly.py + queries.py (that
folder's baseline/recent-window split is unchanged, it's genuinely solid
logic) but reshaped to the spec's candidate contract: {service, signal, z,
timestamp} rather than probe-detector's {service, signal_type, metric,
baseline, current, deviation_score, window}. Signal names are renamed to
match the spec exactly (trace_latency_p95 -> p95_latency, trace_error_rate
-> error_rate, log_error_burst -> log_error_count, resource_cpu -> cpu,
resource_memory -> memory).

"Glances at every service every 5-10s. Shouts the names of any
(service, signal) that looks different from a minute ago. Fast because
it's shallow. Doesn't know how or exactly when." -- spec §2, Tier 1.
"""
from collections import defaultdict
from statistics import mean, stdev

from es_client import esql_rows
from queries import (
    cpu_by_service,
    error_rate_by_service,
    latency_p95_by_service,
    log_error_burst_by_service,
    memory_by_service,
)

Z_SCORE_THRESHOLD = 3.0
RECENT_SECONDS = 60
BUCKET_SECONDS = 30  # must match queries.BUCKET; spec suggests tightening to 5s/5min once tested (see README)
RECENT_BUCKETS = max(1, RECENT_SECONDS // BUCKET_SECONDS)
MIN_BASELINE_POINTS = 4

# (query function, value field in its rows, spec signal name)
SCANS = [
    (error_rate_by_service, "error_rate", "error_rate"),
    (latency_p95_by_service, "p95_ns", "p95_latency"),
    (log_error_burst_by_service, "error_count", "log_error_count"),
    (cpu_by_service, "cpu", "cpu"),
    (memory_by_service, "mem", "memory"),
]


def _score_signal(rows: list[dict], value_field: str, signal: str) -> list[dict]:
    by_service = defaultdict(list)
    for row in rows:
        service = row.get("service.name")
        value = row.get(value_field)
        bucket = row.get("bucket")
        if service is None or value is None or bucket is None:
            continue
        by_service[service].append((bucket, value))

    candidates = []
    for service, points in by_service.items():
        points.sort(key=lambda p: p[0])
        if len(points) < RECENT_BUCKETS + MIN_BASELINE_POINTS:
            continue
        split = len(points) - RECENT_BUCKETS
        baseline_values = [v for _, v in points[:split]]
        recent_points = points[split:]
        recent_values = [v for _, v in recent_points]
        recent_bucket = recent_points[-1][0]
        recent_value = mean(recent_values)
        baseline_mean = mean(baseline_values)
        try:
            baseline_stdev = stdev(baseline_values)
        except (ValueError, ArithmeticError):
            baseline_stdev = 0.0

        if baseline_stdev > 0:
            z = (recent_value - baseline_mean) / baseline_stdev
        elif recent_value > baseline_mean:
            z = Z_SCORE_THRESHOLD + 1
        else:
            z = 0.0

        if z >= Z_SCORE_THRESHOLD:
            candidates.append({
                "service": service,
                "signal": signal,
                "z": round(z, 2),
                "timestamp": str(recent_bucket),
            })
    return candidates


def zscore_scan(verbose: bool = False) -> list[dict]:
    """Run all 5 signals, return every (service, signal) pair whose z-score
    clears the threshold. Over-inclusive on purpose -- no ranking here."""
    candidates = []
    for query_fn, value_field, signal in SCANS:
        try:
            rows = esql_rows(query_fn())
            candidates += _score_signal(rows, value_field, signal)
        except Exception as e:
            if verbose:
                print(f"  [zscore_scan] {query_fn.__name__} failed: {e}")
    return candidates


if __name__ == "__main__":
    import json
    print(json.dumps(zscore_scan(verbose=True), indent=2))
