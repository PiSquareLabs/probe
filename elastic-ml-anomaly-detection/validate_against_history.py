"""Validate the Elastic ML anomaly detection jobs against ground truth this
repo already has, instead of re-injecting all 11 flags live and waiting out
each job's 5-minute bucket span (which would take hours -- see README §3 for
why bucket span matters here).

Both ML jobs' datafeeds backfilled this Elasticsearch instance's entire
trace history the moment they were started (not just "from now" -- Elastic's
default datafeed behavior with no explicit start time is to catch up from
the earliest available document), which means every flagd fault injected
during this project's earlier work (../ml-flag-detection/dataset.csv records
the exact window_start/window_end/label for 65 such windows) already has ML
results sitting in .ml-anomalies-* to check against.

This cross-references every non-baseline window in dataset.csv against
anomaly records for that window's target service, checking whether either
ML job's record_score exceeds a threshold anywhere in
[window_start, window_end + slack]. Slack matters because ML buckets are
5 minutes wide (../README.md #2) while dataset.csv's windows are 25-40s --
a fault needs to move the WHOLE enclosing 5-minute bucket's mean/count
enough to register, so this checks the bucket containing the window and the
one after it, not just an exact-timestamp match.

Usage:
    python validate_against_history.py
"""
import base64
import csv
import json
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START_LOCAL_ENV = REPO_ROOT / "opentelemetry-demo" / "elastic-start-local" / ".env"
DATASET_CSV = REPO_ROOT / "ml-flag-detection" / "dataset.csv"
ES_URL = "http://localhost:9200"

JOBS = ["otel-demo-latency", "otel-demo-error-rate"]
SCORE_THRESHOLD = 10  # record_score; Elastic's own UI treats >=25 as "warning", >=50 "minor+"; using a lower bar here since our injected faults are short relative to the bucket

FLAG_TARGET_SERVICE = {
    "adFailure": "ad", "adHighCpu": "ad", "adManualGc": "ad",
    "cartFailure": "cart", "paymentFailure": "payment",
    "recommendationCacheFailure": "recommendation",
    "imageSlowLoad": "frontend", "intlShippingSlowdown": "shipping",
    "productCatalogFailure": "product-catalog", "emailMemoryLeak": "email",
    "kafkaQueueProblems": None,
}


def _es_password() -> str:
    for line in START_LOCAL_ENV.read_text().splitlines():
        if line.startswith("ES_LOCAL_PASSWORD="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("ES_LOCAL_PASSWORD not found")


_AUTH_HEADER = "Basic " + base64.b64encode(f"elastic:{_es_password()}".encode()).decode()


def _es_search(index: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_search",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": _AUTH_HEADER},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def best_record_score(service: str, window_start: str, window_end: str) -> tuple[float, str]:
    """Max record_score for this service across both jobs, in
    [window_start, window_end + 5 minutes] (one bucket of slack after the
    window closes, since a short fault near the end of a bucket may only
    fully register once that bucket finalizes)."""
    end_with_slack = (
        datetime.fromisoformat(window_end) + timedelta(minutes=5)
    ).isoformat()
    body = {
        "size": 0,
        "query": {"bool": {"filter": [
            {"terms": {"job_id": JOBS}},
            {"term": {"result_type": "record"}},
            {"term": {"partition_field_value": service}},
            {"range": {"timestamp": {"gte": window_start, "lte": end_with_slack}}},
        ]}},
        "aggs": {"top": {"top_hits": {"size": 1, "sort": [{"record_score": "desc"}],
                                       "_source": ["job_id", "record_score", "timestamp"]}}},
        "sort": [{"record_score": "desc"}],
    }
    result = _es_search(".ml-anomalies-*", body)
    # NB: "size": 0 above suppresses the top-level hits.hits array -- the
    # actual best-match document lives in the top_hits sub-aggregation.
    hits = result.get("aggregations", {}).get("top", {}).get("hits", {}).get("hits", [])
    if not hits:
        return 0.0, None
    top = hits[0]["_source"]
    return top["record_score"], top["job_id"]


def main():
    with open(DATASET_CSV, newline="") as f:
        rows = list(csv.DictReader(f))

    by_flag = defaultdict(list)
    for r in rows:
        by_flag[r["label"]].append(r)

    results = []
    print(f"Checking {len(rows) - len(by_flag.get('baseline', []))} fault windows "
          f"across {len(by_flag) - 1} flags against ML anomaly records...\n")

    for flag, target in FLAG_TARGET_SERVICE.items():
        flag_rows = by_flag.get(flag, [])
        if not flag_rows:
            continue
        if target is None:
            print(f"=== {flag}: skipped, no direct target service ===\n")
            results.append({"flag": flag, "target": None, "detected": False, "windows": 0})
            continue

        print(f"=== {flag} (target={target}, {len(flag_rows)} windows) ===")
        window_results = []
        for r in flag_rows:
            score, job = best_record_score(target, r["window_start"], r["window_end"])
            detected = score >= SCORE_THRESHOLD
            window_results.append({"window_start": r["window_start"], "score": score,
                                    "job": job, "detected": detected})
            marker = "DETECTED" if detected else "missed"
            print(f"  {r['window_start']}  score={score:.1f}  {marker}"
                  + (f"  ({job})" if job else ""))

        any_detected = any(w["detected"] for w in window_results)
        best = max(window_results, key=lambda w: w["score"])
        results.append({
            "flag": flag, "target": target, "windows": len(flag_rows),
            "detected": any_detected, "best_score": best["score"],
            "window_details": window_results,
        })
        print()

    detected_count = sum(1 for r in results if r["detected"])
    total = len(results)
    print("=" * 60)
    print(f"Detected in at least one window: {detected_count}/{total} flags")
    print("=" * 60)
    for r in results:
        tag = "DETECTED" if r["detected"] else "MISSED"
        best = f"best_score={r.get('best_score', 0):.1f}" if r["target"] else "n/a"
        print(f"  {r['flag']:28s} target={str(r['target']):18s} {tag:10s} {best}")

    Path("validation_results.json").write_text(json.dumps(results, indent=2))
    print("\nWrote validation_results.json")


if __name__ == "__main__":
    main()
