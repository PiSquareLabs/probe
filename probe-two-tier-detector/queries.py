"""ES|QL query shapes over Elastic's OTel-native data streams.

Ported from ../probe-app/detector/src/detector/queries.py with two changes:

1. Excludes flagd.evaluation.v1.Service/* spans everywhere. flagd's own
   OpenFeature client reconnects a streaming RPC constantly, and those spans
   get tagged status.code == "Error" on every reconnect and have
   multi-minute+ durations (they're long-lived stream connections, not
   requests). Without this filter, every service that resolves flags via
   flagd looks like it has a nonzero baseline error rate and inflated
   latency at all times -- confirmed empirically against this exact stack
   in ../ml-flag-detection/README.md #5 and ../causal-changepoint-detection/
   README.md #1, and re-confirmed by checking status.code=="Error" spans
   for the `ad` service directly:
       2 out of 2 were flagd.evaluation.v1.Service/EventStream, not
       application errors.
2. Adds resource_metric_by_service() -- the original only covered traces
   (error rate, latency) and logs (error burst), not metrics, even though
   the PROBE design doc (../idea/probe.pdf) specifies the Detector should
   scan "traces, metrics, and logs".

Field names confirmed against the live index: `service.name` and
`status.code` work as ECS-style aliases into this OTel-native mapping
(verified with `FROM traces-* | STATS c=COUNT(*) BY service.name`), so the
original file's field choices were already correct -- this only adds the
flagd-noise filter and the metrics query.
"""

WINDOW = "10 minutes"
BUCKET = "30 seconds"
MIN_SAMPLE_COUNT = 3

_EXCLUDE_PROBE = 'NOT service.name LIKE "probe-*"'
_EXCLUDE_FLAGD_NOISE = 'NOT STARTS_WITH(name, "flagd.evaluation")'


def error_rate_by_service() -> str:
    return f"""
FROM traces-*.otel-default
| WHERE @timestamp > NOW() - {WINDOW} AND {_EXCLUDE_PROBE} AND {_EXCLUDE_FLAGD_NOISE}
| EVAL is_error = CASE(status.code == "Error", 1, 0)
| STATS error_rate = AVG(is_error), total = COUNT(*)
    BY service.name, bucket = BUCKET(@timestamp, {BUCKET})
| WHERE total >= {MIN_SAMPLE_COUNT}
| SORT service.name, bucket
"""


def latency_p95_by_service() -> str:
    return f"""
FROM traces-*.otel-default
| WHERE @timestamp > NOW() - {WINDOW} AND {_EXCLUDE_PROBE} AND {_EXCLUDE_FLAGD_NOISE}
| STATS p95_ns = PERCENTILE(duration, 95), total = COUNT(*)
    BY service.name, bucket = BUCKET(@timestamp, {BUCKET})
| WHERE total >= {MIN_SAMPLE_COUNT}
| SORT service.name, bucket
"""


def log_error_burst_by_service() -> str:
    return f"""
FROM logs-*.otel-default
| WHERE @timestamp > NOW() - {WINDOW} AND severity_text IN ("ERROR", "FATAL")
    AND NOT resource.attributes.service.name LIKE "probe-*"
| STATS error_count = COUNT(*)
    BY service.name = resource.attributes.service.name, bucket = BUCKET(@timestamp, {BUCKET})
| SORT service.name, bucket
"""


def resource_metric_by_service(metric_exprs: list[str], out_field: str) -> str:
    """Build a query for one resource metric (CPU or memory), COALESCE-d
    across the language-runtime-specific field names that actually carry it
    -- not every OTel SDK emits the same metric name (see
    ../ml-flag-detection/README.md #2 for the full field survey: process.*
    for Go/Node, jvm.* for Java, go.memory.used for Go's own runtime
    metrics, process.runtime.cpython.* for Python)."""
    # COALESCE requires every argument to share one type; the underlying
    # metrics are a mix of long and double depending on the OTel SDK that
    # emitted them (e.g. metrics.v8js.memory.heap.used is double while
    # metrics.go.memory.used is long), so cast them all to double first.
    coalesce_expr = "COALESCE(" + ", ".join(f"TO_DOUBLE({e})" for e in metric_exprs) + ")"
    return f"""
FROM metrics-*.otel-default
| WHERE @timestamp > NOW() - {WINDOW} AND {_EXCLUDE_PROBE}
| EVAL m = {coalesce_expr}
| STATS {out_field} = AVG(m), total = COUNT(*)
    BY service.name, bucket = BUCKET(@timestamp, {BUCKET})
| WHERE {out_field} IS NOT NULL AND total >= {MIN_SAMPLE_COUNT}
| SORT service.name, bucket
"""


def cpu_by_service() -> str:
    return resource_metric_by_service(
        [
            "metrics.process.cpu.utilization",
            "metrics.jvm.cpu.recent_utilization",
            "metrics.process.runtime.cpython.cpu.utilization",
        ],
        "cpu",
    )


def memory_by_service() -> str:
    return resource_metric_by_service(
        [
            "metrics.process.memory.usage",
            "metrics.jvm.memory.used",
            "metrics.go.memory.used",
            "metrics.process.runtime.cpython.memory",
            "metrics.v8js.memory.heap.used",
        ],
        "mem",
    )
