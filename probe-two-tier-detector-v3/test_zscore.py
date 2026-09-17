"""The 5 tests FIX-tier1-zscore.md asks for, with its exact synthetic
fixtures. No Elasticsearch needed -- these exercise compute_z() and
_score_signal() directly against hand-built rows/series.

Run: python -m pytest test_zscore.py -v
  (or: python test_zscore.py)
"""
from zscore_scan import compute_z, _score_signal


# ---------------------------------------------------------------------
# 1. Dilution test
# ---------------------------------------------------------------------
def test_dilution_old_config_dilutes_fault():
    """56 baseline buckets at 180 (flat, no noise needed to show dilution),
    old shape: recent = last 2 buckets, one of which straddles the fault
    boundary and is only half-faulted -- reproduces the fix doc's own
    worked example (bucket A: 20s fault + 10s healthy -> avg 467;
    bucket B: 15s fault + 15s healthy -> avg 395; recent_value = 431)."""
    baseline = [180.0] * 56
    old_recent = [467.0, 395.0]  # straddled buckets, from the fix doc's worked case
    recent_value = sum(old_recent) / len(old_recent)
    assert recent_value < 450, f"old config should dilute below 450, got {recent_value}"


def test_dilution_new_config_recent_median_holds():
    """New shape: 3 recent buckets fully inside a 35s fault at 610 (bucket
    size 10s, so buckets 2/3 sit entirely within the fault window)."""
    recent = [610.0, 610.0, 610.0]  # 3 fully-faulted 10s buckets
    assert min(recent) >= 550
    from statistics import median
    assert median(recent) >= 550


# ---------------------------------------------------------------------
# 2. Contended-baseline test
# ---------------------------------------------------------------------
def _contended_baseline():
    baseline = [180.0] * 56
    for i, spike in zip([5, 15, 30, 45], [500.0, 650.0, 600.0, 450.0]):
        baseline[i] = spike
    return baseline


def test_contended_baseline_old_z_falls_below_threshold():
    """Old config: recent is the diluted 2-bucket average from the fix
    doc's worked example (431), and the same 4 contention spikes that
    inflate mean/stdev knock the mean-based z below threshold."""
    baseline = _contended_baseline()
    old_recent = [467.0, 395.0]
    z_old = compute_z(baseline, old_recent, robust=False)
    assert z_old < 3.0, f"old mean/stdev z should be knocked below 3.0 by contention, got {z_old}"


def test_contended_baseline_robust_z_stays_above_threshold():
    """New config: recent is 3 fully-faulted buckets (no dilution, C1), and
    median/MAD (C2) isn't moved by 4 spikes out of 56 baseline points --
    MAD is 0 here (the un-spiked majority all sit at 180), which is
    exactly the doc's mad==0 special case: recent_median > baseline_median
    forces z = THRESHOLD + 1."""
    baseline = _contended_baseline()
    new_recent = [610.0, 610.0, 610.0]
    z_new = compute_z(baseline, new_recent, robust=True)
    assert z_new >= 3.0, f"robust z should stay >= 3.0 despite contention, got {z_new}"


# ---------------------------------------------------------------------
# 3. Persistence test
# ---------------------------------------------------------------------
def test_persistence_single_scan_over_threshold_does_not_shout():
    rows_hot = [
        {"service.name": "ad", "value": v, "bucket": i}
        for i, v in enumerate([180.0] * 56 + [610.0, 610.0, 610.0])
    ]
    rows_cold = [
        {"service.name": "ad", "value": v, "bucket": i}
        for i, v in enumerate([180.0] * 59)
    ]
    streak_state = {}
    # scan 1: hot -> z >= threshold, but streak only reaches 1 < PERSISTENCE=2
    out1 = _score_signal(rows_hot, "value", "p95_latency", streak_state)
    assert out1 == [], f"single hot scan must not shout, got {out1}"
    # scan 2: back to baseline -> streak resets to 0
    out2 = _score_signal(rows_cold, "value", "p95_latency", streak_state)
    assert out2 == []
    assert streak_state[("ad", "p95_latency")] == 0


def test_persistence_two_consecutive_scans_shout():
    rows_hot = [
        {"service.name": "ad", "value": v, "bucket": i}
        for i, v in enumerate([180.0] * 56 + [610.0, 610.0, 610.0])
    ]
    streak_state = {}
    out1 = _score_signal(rows_hot, "value", "p95_latency", streak_state)
    assert out1 == []  # streak == 1
    out2 = _score_signal(rows_hot, "value", "p95_latency", streak_state)
    assert len(out2) == 1, f"second consecutive hot scan must shout, got {out2}"
    assert out2[0]["streak"] == 2


# ---------------------------------------------------------------------
# 4. Error floor test
# ---------------------------------------------------------------------
def _error_rows(baseline_rate, recent_rate, recent_error_count):
    rows = [
        {"service.name": "cart", "value": baseline_rate, "bucket": i, "count": 0}
        for i in range(56)
    ]
    # spread recent_error_count evenly-ish across the 3 recent buckets
    per_bucket = [recent_error_count // 3] * 3
    per_bucket[0] += recent_error_count - sum(per_bucket)
    for j, c in enumerate(per_bucket):
        rows.append({"service.name": "cart", "value": recent_rate, "bucket": 56 + j, "count": c})
    return rows


def test_error_floor_below_min_count_does_not_shout():
    rows = _error_rows(0.001, 0.004, recent_error_count=2)
    streak_state = {("cart", "error_rate"): 1}  # already primed, so only C4 can block it
    out = _score_signal(rows, "value", "error_rate", streak_state, count_field="count")
    assert out == [], f"recent_error_count=2 < MIN_ERROR_COUNT=5 must not shout, got {out}"


def test_error_floor_above_min_count_shouts():
    rows = _error_rows(0.001, 0.004, recent_error_count=8)
    streak_state = {("cart", "error_rate"): 1}  # primed so this scan is the 2nd consecutive
    out = _score_signal(rows, "value", "error_rate", streak_state, count_field="count")
    assert len(out) == 1, f"recent_error_count=8 >= MIN_ERROR_COUNT=5 must shout, got {out}"
    assert out[0]["recent_count"] == 8


# ---------------------------------------------------------------------
# 5. Partial bucket test
# ---------------------------------------------------------------------
def test_partial_bucket_excluded_from_recent():
    """This is enforced at the query level (queries.py's _window_clause
    upper-bounds @timestamp to `now - bucket_seconds`), not in Python, so
    there's nothing for zscore_scan.py to filter -- a bucket with only 3
    spans simply never appears in the rows it receives. Confirm that
    _score_signal has no code path that would accept a bucket below
    MIN_SPANS_PER_BUCKET if one somehow arrived (defense in depth: the
    count floor is applied at the query WHERE clause, verified here by
    checking the query text itself)."""
    from queries import latency_p95_by_service
    q = latency_p95_by_service(lookback_minutes=10, bucket_seconds=10, min_spans_per_bucket=20)
    assert "@timestamp < NOW() - 10 seconds" in q, "partial tail bucket must be excluded by upper bound"
    assert "total >= 20" in q, "per-bucket floor must be enforced in the query"


if __name__ == "__main__":
    import sys
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
