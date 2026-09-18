"""Tier 1 z-score scan, v2 -- implements ../FIX-tier1-zscore.md C1-C4.
(C5 was already true of this codebase's latency query -- see queries.py's
docstring -- so there's nothing to change for it here.)

v1 (../probe-two-tier-detector/zscore_scan.py) scored 2/11 on the full
validation battery, with every miss being Tier 1 never shouting. The fix
doc's diagnosis, reproduced in this module's design:

  C1 - the old 60s "recent" window (2x30s buckets) diluted a 25-40s fault
       with healthy seconds from the same bucket. Now: 10s buckets,
       3 recent buckets (30s), and the still-filling tail bucket is
       excluded at the query level (queries.py's upper timestamp bound).
  C2 - mean/stdev is inflated by host contention spikes sitting in the
       baseline; median/MAD isn't moved by them. THRESHOLD keeps meaning
       via the 0.6745 constant that makes MAD comparable to stdev on
       roughly-Gaussian data.
  C3 - 60 independent (service, signal) checks every ~10s means real
       false shouts even at z >= 3 on noisy, heavy-tailed data. Requiring
       PERSISTENCE consecutive scans over threshold before shouting is
       what filters those out -- a real fault stays elevated across
       several scans, a noise spike usually doesn't.
  C4 - a near-zero baseline error rate makes z enormous from two or three
       extra errors. A floor on the *raw count*, not just the rate,
       stops that without touching the latency/CPU/memory checks.

Persistence (C3) needs state that survives across scans, which only
makes sense for a long-running process, not a fresh `python detector.py`
invocation each time -- see detector.py's --loop mode. zscore_scan() takes
that state as an explicit dict argument (mutated in place) rather than a
hidden module-level global, so it's easy to test and easy to reset.
"""
from collections import defaultdict
from statistics import median

from es_client import esql_rows
from queries import (
    cpu_by_service,
    error_rate_by_service,
    latency_p95_by_service,
    log_error_burst_by_service,
    memory_by_service,
)

# --- config surface (FIX-tier1-zscore.md "Config surface") ---
BUCKET_SECONDS = 10
LOOKBACK_MINUTES = 10
RECENT_BUCKETS = 3
MIN_SPANS_PER_BUCKET = 20
THRESHOLD = 3.0
PERSISTENCE = 2
MIN_ERROR_COUNT = 5
ROBUST_Z = True  # False -> old mean/stdev path, for A/B (fix doc C2)

MIN_BASELINE_POINTS = 4
_MAD_TO_STDEV = 0.6745  # makes MAD comparable to stdev for ~Gaussian data

# signals that get the C4 minimum-raw-count floor (not latency/CPU/memory)
_COUNT_FLOORED_SIGNALS = {"error_rate", "log_error_count"}


def _median_mad_z(baseline_values: list[float], recent_values: list[float]) -> float:
    baseline_median = median(baseline_values)
    mad = median([abs(v - baseline_median) for v in baseline_values])
    recent_median = median(recent_values)
    if mad == 0:
        if recent_median > baseline_median:
            return THRESHOLD + 1
        return 0.0
    return _MAD_TO_STDEV * (recent_median - baseline_median) / mad


def _mean_stdev_z(baseline_values: list[float], recent_values: list[float]) -> float:
    from statistics import mean, stdev
    baseline_mean = mean(baseline_values)
    recent_value = mean(recent_values)
    try:
        baseline_stdev = stdev(baseline_values)
    except (ValueError, ArithmeticError):
        baseline_stdev = 0.0
    if baseline_stdev > 0:
        return (recent_value - baseline_mean) / baseline_stdev
    if recent_value > baseline_mean:
        return THRESHOLD + 1
    return 0.0


def compute_z(baseline_values: list[float], recent_values: list[float],
              robust: bool = ROBUST_Z) -> float:
    """Exposed standalone for the dilution/contended-baseline unit tests
    in test_zscore.py -- both take plain baseline/recent value lists, no
    Elasticsearch involved."""
    if robust:
        return _median_mad_z(baseline_values, recent_values)
    return _mean_stdev_z(baseline_values, recent_values)


def _score_signal(rows: list[dict], value_field: str, signal: str,
                   streak_state: dict, count_field: str = None,
                   recent_buckets: int = RECENT_BUCKETS,
                   min_baseline_points: int = MIN_BASELINE_POINTS,
                   threshold: float = THRESHOLD,
                   persistence: int = PERSISTENCE,
                   min_error_count: int = MIN_ERROR_COUNT,
                   robust: bool = ROBUST_Z) -> list[dict]:
    by_service = defaultdict(list)
    for row in rows:
        service = row.get("service.name")
        value = row.get(value_field)
        bucket = row.get("bucket")
        if service is None or value is None or bucket is None:
            continue
        count = row.get(count_field) if count_field else None
        by_service[service].append((bucket, value, count))

    candidates = []
    for service, points in by_service.items():
        points.sort(key=lambda p: p[0])
        if len(points) < recent_buckets + min_baseline_points:
            continue
        split = len(points) - recent_buckets
        baseline_values = [v for _, v, _ in points[:split]]
        recent_points = points[split:]
        recent_values = [v for _, v, _ in recent_points]
        recent_bucket = recent_points[-1][0]

        z = compute_z(baseline_values, recent_values, robust=robust)

        key = (service, signal)
        if z >= threshold:
            streak_state[key] = streak_state.get(key, 0) + 1
        else:
            streak_state[key] = 0
            continue

        if streak_state[key] < persistence:
            continue  # C3: not persistent enough yet

        if signal in _COUNT_FLOORED_SIGNALS:
            recent_count = sum(c or 0 for _, _, c in recent_points)
            if recent_count < min_error_count:
                continue  # C4: rate looks anomalous, but too few raw events to trust it
        else:
            recent_count = None

        candidates.append({
            "service": service,
            "signal": signal,
            "z": round(z, 2),
            "timestamp": str(recent_bucket),
            "streak": streak_state[key],
            "recent_count": recent_count,
        })
    return candidates


def zscore_scan(streak_state: dict, verbose: bool = False,
                 bucket_seconds: int = BUCKET_SECONDS,
                 lookback_minutes: int = LOOKBACK_MINUTES,
                 min_spans_per_bucket: int = MIN_SPANS_PER_BUCKET,
                 min_error_count: int = MIN_ERROR_COUNT) -> list[dict]:
    """Run all 5 signals, return every (service, signal) pair whose z-score
    has cleared THRESHOLD for PERSISTENCE consecutive calls with this same
    streak_state dict. Pass a fresh {} to disable persistence across calls
    (each candidate then needs PERSISTENCE=1 to ever shout -- set that
    explicitly if you want single-scan behavior for comparison).

    min_error_count was previously only a _score_signal() default, not
    reachable from here -- found live while testing adFailure (~10%
    error rate) against modest traffic: the C4 floor (>=5 raw errors in
    the recent 30s window) never cleared, since 10% of ~10 req/10s-bucket
    is under 1 error per bucket. Threading it through lets a caller lower
    it for exactly this situation -- a real signal thinner than the
    floor's original noise-suppression target -- without editing this
    file's own module-level default, which stays correctly conservative.
    """
    scans = [
        (error_rate_by_service(lookback_minutes, bucket_seconds, min_spans_per_bucket),
         "error_rate", "error_rate", "fail_count"),
        (latency_p95_by_service(lookback_minutes, bucket_seconds, min_spans_per_bucket),
         "p95_ns", "p95_latency", None),
        (log_error_burst_by_service(lookback_minutes, bucket_seconds),
         "error_count", "log_error_count", "error_count"),
        (cpu_by_service(lookback_minutes, bucket_seconds), "cpu", "cpu", None),
        (memory_by_service(lookback_minutes, bucket_seconds), "mem", "memory", None),
    ]
    candidates = []
    for query, value_field, signal, count_field in scans:
        try:
            rows = esql_rows(query)
            candidates += _score_signal(rows, value_field, signal, streak_state, count_field,
                                         min_error_count=min_error_count)
        except Exception as e:
            if verbose:
                print(f"  [zscore_scan] {signal} failed: {e}")
    return candidates


if __name__ == "__main__":
    import json
    state = {}
    print(json.dumps(zscore_scan(state, verbose=True), indent=2))
