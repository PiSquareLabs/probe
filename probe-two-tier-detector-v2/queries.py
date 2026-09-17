"""Tier 1 ES|QL query shapes, v2 -- per ../FIX-tier1-zscore.md C1, C4, C5.

Changes from ../probe-two-tier-detector/queries.py (itself copied from
../probe-detector/queries.py):

- C1: bucket size and lookback are now parameters, not fixed at 30s/10min.
  The default is 10s buckets, matching the fix doc's config surface. The
  query also excludes the most recent, still-filling bucket (@timestamp
  upper-bounded to `now - bucket_seconds`) -- a 5-second-old bucket
  averages a handful of spans and is noise, not signal.
- C1: the per-bucket sample floor is now MIN_SPANS_PER_BUCKET (default 20,
  up from 3) for the trace-based queries (latency, error rate) specifically
  -- "spans" doesn't map cleanly onto log/metric document counts, so those
  keep the original, lower floor.
- C4: error_rate_by_service() now also returns fail_count (SUM(is_error),
  the raw number of failed spans in the bucket) alongside the rate, so
  zscore_scan.py can apply a minimum-count floor on top of the z-score --
  a rate near zero with a near-zero baseline produces enormous z from two
  or three extra errors, and the floor stops that without touching latency.
- C5: latency was already PERCENTILE(duration, 95) in the copied file, not
  a mean -- the fix doc's C5 assumption (per-bucket mean) doesn't apply to
  this codebase. Left as-is; see README.md #2 for this discrepancy.

Field names and the flagd.evaluation.v1.Service/* exclusion are unchanged
from ../probe-detector/queries.py (see that file's docstring for how the
flagd-noise bug was found).
"""

_EXCLUDE_PROBE = 'NOT service.name LIKE "probe-*"'
_EXCLUDE_FLAGD_NOISE = 'NOT STARTS_WITH(name, "flagd.evaluation")'


def _window_clause(lookback_minutes: int, bucket_seconds: int) -> str:
    """WHERE clause bounding the query to [now - lookback, now - bucket)
    -- the upper bound drops the in-progress tail bucket (C1)."""
    return (
        f"@timestamp > NOW() - {lookback_minutes} minutes "
        f"AND @timestamp < NOW() - {bucket_seconds} seconds"
    )


def error_rate_by_service(lookback_minutes: int = 10, bucket_seconds: int = 10,
                           min_spans_per_bucket: int = 20) -> str:
    return f"""
FROM traces-*.otel-default
| WHERE {_window_clause(lookback_minutes, bucket_seconds)} AND {_EXCLUDE_PROBE} AND {_EXCLUDE_FLAGD_NOISE}
| EVAL is_error = CASE(status.code == "Error", 1, 0)
| STATS error_rate = AVG(is_error), fail_count = SUM(is_error), total = COUNT(*)
    BY service.name, bucket = BUCKET(@timestamp, {bucket_seconds} seconds)
| WHERE total >= {min_spans_per_bucket}
| SORT service.name, bucket
"""


def latency_p95_by_service(lookback_minutes: int = 10, bucket_seconds: int = 10,
                            min_spans_per_bucket: int = 20) -> str:
    return f"""
FROM traces-*.otel-default
| WHERE {_window_clause(lookback_minutes, bucket_seconds)} AND {_EXCLUDE_PROBE} AND {_EXCLUDE_FLAGD_NOISE}
| STATS p95_ns = PERCENTILE(duration, 95), total = COUNT(*)
    BY service.name, bucket = BUCKET(@timestamp, {bucket_seconds} seconds)
| WHERE total >= {min_spans_per_bucket}
| SORT service.name, bucket
"""


def log_error_burst_by_service(lookback_minutes: int = 10, bucket_seconds: int = 10) -> str:
    return f"""
FROM logs-*.otel-default
| WHERE {_window_clause(lookback_minutes, bucket_seconds)} AND severity_text IN ("ERROR", "FATAL")
    AND NOT resource.attributes.service.name LIKE "probe-*"
| STATS error_count = COUNT(*)
    BY service.name = resource.attributes.service.name, bucket = BUCKET(@timestamp, {bucket_seconds} seconds)
| SORT service.name, bucket
"""


def resource_metric_by_service(metric_exprs: list[str], out_field: str,
                                lookback_minutes: int = 10, bucket_seconds: int = 10) -> str:
    coalesce_expr = "COALESCE(" + ", ".join(f"TO_DOUBLE({e})" for e in metric_exprs) + ")"
    return f"""
FROM metrics-*.otel-default
| WHERE {_window_clause(lookback_minutes, bucket_seconds)} AND {_EXCLUDE_PROBE}
| EVAL m = {coalesce_expr}
| STATS {out_field} = AVG(m), total = COUNT(*)
    BY service.name, bucket = BUCKET(@timestamp, {bucket_seconds} seconds)
| WHERE {out_field} IS NOT NULL AND total >= 3
| SORT service.name, bucket
"""


def cpu_by_service(lookback_minutes: int = 10, bucket_seconds: int = 10) -> str:
    return resource_metric_by_service(
        [
            "metrics.process.cpu.utilization",
            "metrics.jvm.cpu.recent_utilization",
            "metrics.process.runtime.cpython.cpu.utilization",
        ],
        "cpu", lookback_minutes, bucket_seconds,
    )


def memory_by_service(lookback_minutes: int = 10, bucket_seconds: int = 10) -> str:
    return resource_metric_by_service(
        [
            "metrics.process.memory.usage",
            "metrics.jvm.memory.used",
            "metrics.go.memory.used",
            "metrics.process.runtime.cpython.memory",
            "metrics.v8js.memory.heap.used",
        ],
        "mem", lookback_minutes, bucket_seconds,
    )
