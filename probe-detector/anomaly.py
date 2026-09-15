"""Client-side z-score anomaly scoring over ES|QL bucketed rows.

Ported unchanged (down to the docstring reasoning) from
../probe-app/detector/src/detector/anomaly.py -- the baseline/recent split
logic here is solid and didn't need fixing, only the queries feeding it did
(see queries.py's module docstring).

ES|QL's CHANGE_POINT command may not be available on every Elastic
Serverless project tier -- verify at Stage 0/1 start. This z-score fallback
works regardless of tier, so it's the default rather than an afterthought.
(On this project's self-hosted Elasticsearch 9.5.3, CHANGE_POINT *is*
available and was used directly in ../causal-changepoint-detection/ --
z-score here is a legitimate, portable alternative worth having either way.)
"""

from collections import defaultdict
from statistics import mean, stdev

Z_SCORE_THRESHOLD = 3.0

# How far back "recent" looks, independent of how long the total lookback
# (queries.WINDOW) is. Keeping this fixed and small is what makes detection
# fast: right after a fault starts, only a couple of buckets are affected,
# and averaging them with a handful of siblings (not the whole window) lets
# the fault dominate the average almost immediately.
RECENT_SECONDS = 60
BUCKET_SECONDS = 30  # must match queries.BUCKET
RECENT_BUCKETS = max(1, RECENT_SECONDS // BUCKET_SECONDS)

# How much clean history the baseline needs behind the recent window before
# we trust its mean/stdev at all.
MIN_BASELINE_POINTS = 4


def score_anomalies(rows: list[dict], value_field: str, signal_type: str, metric_name: str) -> list[dict]:
    """Group rows by service.name, split each service's points into a
    baseline (everything except the most recent RECENT_BUCKETS buckets) and
    a recent window (the last RECENT_BUCKETS buckets), and return anomalies
    whose z-score exceeds the threshold.

    This used to split the window 50/50 into "earlier half" / "later half".
    That was a real fix for one problem -- a single trailing bucket as
    "recent" means every bucket immediately re-joins the baseline the
    instant a newer bucket arrives, so a fault that's been active for more
    than one bucket poisons its own baseline and the z-score collapses back
    toward zero right when the signal should be strongest.

    But tying "recent" to half of the *entire* lookback window (queries.WINDOW)
    means recent is only as small as the window is short, and the window
    can't be made much longer without also making detection slower: a
    5-minute window with a 50/50 split means "recent" is a 2.5-minute
    average, so a fault that started 5-10 seconds ago is diluted across
    150 seconds of mostly-clean history and the z-score barely moves --
    exactly the "not accurate and not timely" symptom.

    The fix is to decouple the two: keep "recent" a small, fixed window
    (RECENT_BUCKETS, independent of total WINDOW) so a fresh fault shows up
    fast, and let queries.WINDOW be as long as needed to keep the *baseline*
    (everything before that small recent window) large and clean. A longer
    WINDOW no longer costs any reaction time -- it only buys the baseline
    more history.
    """
    by_service = defaultdict(list)
    for row in rows:
        service = row.get("service.name")
        value = row.get(value_field)
        bucket = row.get("bucket")
        if service is None or value is None or bucket is None:
            continue
        by_service[service].append((bucket, value))

    anomalies = []
    for service, points in by_service.items():
        points.sort(key=lambda p: p[0])
        if len(points) < RECENT_BUCKETS + MIN_BASELINE_POINTS:
            continue  # not enough clean history behind the recent window
        split = len(points) - RECENT_BUCKETS
        baseline_points, recent_points = points[:split], points[split:]
        baseline_values = [v for _, v in baseline_points]
        recent_values = [v for _, v in recent_points]
        recent_bucket = recent_points[-1][0]
        recent_value = mean(recent_values)
        baseline_mean = mean(baseline_values)
        try:
            baseline_stdev = stdev(baseline_values)
        except (ValueError, ArithmeticError):
            baseline_stdev = 0.0

        if baseline_stdev > 0:
            z_score = (recent_value - baseline_mean) / baseline_stdev
        elif recent_value > baseline_mean:
            z_score = Z_SCORE_THRESHOLD + 1  # baseline was flat, recent value isn't
        else:
            z_score = 0.0

        if z_score >= Z_SCORE_THRESHOLD:
            anomalies.append(
                {
                    "service": service,
                    "signal_type": signal_type,
                    "metric": metric_name,
                    "baseline": round(baseline_mean, 4),
                    "current": round(recent_value, 4),
                    "deviation_score": round(z_score, 2),
                    "window": str(recent_bucket),
                }
            )
    return anomalies
