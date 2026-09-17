"""Run the trained classifier against a fresh live telemetry window and
bundle its prediction with the two things this project's model alone
doesn't produce: a log-based signal, and the service dependency graph.

The model itself was trained only on trace+metric features (see
collect_dataset.py) -- retraining it on log features would need a full
re-collection pass, which this script deliberately does not do. Instead,
it adds a live, non-training-feature log-error-burst check as
*supplementary evidence* attached to the output, and looks up which
service the predicted flag (if not "baseline") targets via the same
service dependency graph ../causal-changepoint-detection/ derived
empirically -- reused here via a plain file copy, not a dependency between
the two projects' code.

This module only produces the shareable result described above; it does
not call or depend on a Remediator.

Usage:
    python predict_and_bundle.py --out result.json

Targets the local self-hosted Elasticsearch by default; override with
ES_URL + ES_API_KEY (or ES_USERNAME/ES_PASSWORD) to point at Elastic
Cloud/Serverless instead -- see ../RUNNING_ON_ELASTIC_CLOUD.md.
"""
import argparse
import base64
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd

from collect_dataset import (
    CYCLE_DURATION,
    query_resource_features,
    query_trace_features,
    flatten_window,
    START_LOCAL_ENV,
)

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")

# Which service each flag's fault targets -- same mapping used by every
# other detector's validation harness in this repo, for a consistent
# "flag -> service" lookup when bundling the dependency graph.
FLAG_TARGET_SERVICE = {
    "adFailure": "ad", "adHighCpu": "ad", "adManualGc": "ad",
    "cartFailure": "cart", "paymentFailure": "payment",
    "recommendationCacheFailure": "recommendation",
    "imageSlowLoad": "frontend", "intlShippingSlowdown": "shipping",
    "productCatalogFailure": "product-catalog", "emailMemoryLeak": "email",
    "kafkaQueueProblems": None,
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def log_error_summary(start_iso: str, end_iso: str) -> list[dict]:
    """Supplementary, non-training-feature signal: raw ERROR/FATAL log
    counts per service in the same window the model just scored. Not fed
    into the classifier -- just attached to the result so a downstream
    consumer sees log evidence alongside the model's trace/metric-based
    prediction, same as the other two detectors' log signals."""
    query = (
        f'FROM logs-*.otel-default '
        f'| WHERE @timestamp >= "{start_iso}" AND @timestamp < "{end_iso}" '
        f'AND severity_text IN ("ERROR", "FATAL") '
        f'| STATS error_count = COUNT(*) BY service.name '
        f'| SORT error_count DESC'
    )
    try:
        result = _esql(query)
    except Exception:
        return []
    cols = [c["name"] for c in result.get("columns", [])]
    return [dict(zip(cols, v)) for v in result.get("values", [])]


def load_graph_edges() -> list[dict]:
    graph_path = Path(__file__).parent / "service_graph.json"
    return json.loads(graph_path.read_text())["edges"]


def predict_once(model_path: str) -> dict:
    bundle = joblib.load(model_path)
    clf, feature_cols = bundle["model"], bundle["feature_cols"]

    start = datetime.now(timezone.utc)
    time.sleep(CYCLE_DURATION)
    end = datetime.now(timezone.utc)
    time.sleep(3)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    trace_feats = query_trace_features(start_iso, end_iso)
    resource_feats = query_resource_features(start_iso, end_iso)
    row = flatten_window("?", trace_feats, resource_feats)

    X = pd.DataFrame([{c: row.get(c, 0.0) for c in feature_cols}])
    pred = clf.predict(X)[0]
    proba = dict(zip(clf.classes_, clf.predict_proba(X)[0]))

    logs = log_error_summary(start_iso, end_iso)

    return {
        "detector": "ml-flag-detection",
        "generated_at": end.isoformat(),
        "window_start": start_iso,
        "window_end": end_iso,
        "predicted_label": pred,
        "class_probabilities": {k: round(float(v), 4) for k, v in
                                 sorted(proba.items(), key=lambda kv: -kv[1])},
        "log_error_summary": logs,
        "service_dependency_graph": {"edges": load_graph_edges()},
        "named_root_cause": (
            FLAG_TARGET_SERVICE.get(pred) if pred != "baseline" else None
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="model.joblib")
    ap.add_argument("--out", default=None, help="write the shareable result JSON to this path")
    args = ap.parse_args()

    result = predict_once(args.model)

    print(f"Predicted: {result['predicted_label']}  "
          f"(named_root_cause={result['named_root_cause']})")
    print("Top probabilities:")
    for label, p in list(result["class_probabilities"].items())[:5]:
        print(f"  {label:28s} {p:.3f}")
    if result["log_error_summary"]:
        print("\nLog error counts in this window:")
        for row in result["log_error_summary"]:
            print(f"  {row['service.name']:18s} {row['error_count']}")
    else:
        print("\nNo ERROR/FATAL logs in this window.")

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))
        print(f"\nWrote shareable result to {args.out}")


if __name__ == "__main__":
    main()
