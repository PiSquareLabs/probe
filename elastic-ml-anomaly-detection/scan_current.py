"""Query the two running Elastic ML jobs for recent anomaly records and
bundle the result in the same shareable schema the other three detectors
in this repo use (service_dependency_graph + anomalies + named_root_cause),
for a downstream Correlator/Remediator. This module only produces that
result; it does not call, know about, or depend on any Remediator.

Usage:
    python scan_current.py --minutes 30 --out result.json
"""
import argparse
import base64
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START_LOCAL_ENV = REPO_ROOT / "opentelemetry-demo" / "elastic-start-local" / ".env"
ES_URL = "http://localhost:9200"
JOBS = ["otel-demo-latency", "otel-demo-error-rate"]


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


def recent_records(minutes: int, min_score: float = 3.0) -> list[dict]:
    body = {
        "size": 100,
        "query": {"bool": {"filter": [
            {"terms": {"job_id": JOBS}},
            {"term": {"result_type": "record"}},
            {"range": {"timestamp": {"gte": f"now-{minutes}m"}}},
            {"range": {"record_score": {"gte": min_score}}},
        ]}},
        "sort": [{"record_score": "desc"}],
    }
    result = _es_search(".ml-anomalies-*", body)
    return [h["_source"] for h in result.get("hits", {}).get("hits", [])]


def load_graph_edges() -> list[dict]:
    graph_path = Path(__file__).parent.parent / "causal-changepoint-detection" / "service_graph.json"
    return json.loads(graph_path.read_text())["edges"]


def build_result(records: list[dict], minutes: int) -> dict:
    anomalies = [
        {
            "service": r["partition_field_value"],
            "job": r["job_id"],
            "function": r["function"],
            "record_score": r["record_score"],
            "typical": r.get("typical"),
            "actual": r.get("actual"),
            "timestamp": r["timestamp"],
        }
        for r in records
    ]
    return {
        "detector": "elastic-ml-anomaly-detection",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_minutes": minutes,
        "jobs": JOBS,
        "service_dependency_graph": {"edges": load_graph_edges()},
        "anomalies": anomalies,
        "named_root_cause": None,  # no ranking layer here -- see README's Correlator note
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--min-score", type=float, default=3.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    records = recent_records(args.minutes, args.min_score)
    result = build_result(records, args.minutes)

    if not records:
        print(f"No ML anomaly records (score >= {args.min_score}) in the last {args.minutes} minutes.")
    else:
        print(f"{len(records)} anomaly record(s):\n")
        for r in records:
            print(f"  score={r['record_score']:6.1f}  {r['partition_field_value']:18s} "
                  f"{r['function']:12s} job={r['job_id']}")

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))
        print(f"\nWrote shareable result to {args.out}")


if __name__ == "__main__":
    main()
