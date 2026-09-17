"""The Detector, per ../idea/probe.pdf: "Scans live telemetry in
Elasticsearch for statistically significant anomalies across traces,
metrics, and logs as an incident unfolds."

Deliberately over-inclusive: this returns every anomalous (service, signal)
pair whose z-score clears the threshold, with no attempt to pick a single
root cause -- that's the Correlator's job (see ../idea/probe.pdf's
architecture: Detector -> Correlator -> Remediator). This module only
implements the Detector stage, run standalone against a live Elasticsearch
rather than as the FastAPI service in ../probe-app/detector (which targets
Elastic Cloud/Serverless via API key; this targets the local self-hosted
cluster with the same query/scoring logic, for quick command-line use and
for the validation harness in validate_against_demo.py).

Usage:
    python detector.py
"""
import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from anomaly import score_anomalies
from es_client import esql_rows
from queries import (
    cpu_by_service,
    error_rate_by_service,
    latency_p95_by_service,
    log_error_burst_by_service,
    memory_by_service,
)

SCANS = [
    (error_rate_by_service, "error_rate", "trace_error_rate", "error_rate"),
    (latency_p95_by_service, "p95_ns", "trace_latency_p95", "duration_p95_ns"),
    (log_error_burst_by_service, "error_count", "log_error_burst", "error_count"),
    (cpu_by_service, "cpu", "resource_cpu", "cpu_utilization"),
    (memory_by_service, "mem", "resource_memory", "memory_bytes"),
]


def scan(verbose: bool = False) -> list[dict]:
    """Run the fixed battery of anomaly queries and return a raw,
    deliberately over-inclusive candidate list."""
    anomalies = []
    for query_fn, value_field, signal_type, metric_name in SCANS:
        try:
            rows = esql_rows(query_fn())
            anomalies += score_anomalies(rows, value_field, signal_type, metric_name)
        except Exception as e:
            if verbose:
                logging.warning("%s query failed: %s", query_fn.__name__, e)
    return anomalies


def load_graph_edges() -> list[dict]:
    graph_path = Path(__file__).parent / "service_graph.json"
    return json.loads(graph_path.read_text())["edges"]


def build_result(anomalies: list[dict]) -> dict:
    """The shareable output of this detector. Deliberately has no
    "named_root_cause" field: per ../idea/probe.pdf's architecture this
    detector's job stops at an over-inclusive candidate list (traces,
    metrics, AND logs -- log_error_burst is one of the SCANS above) plus
    the service dependency graph a downstream Correlator/Remediator would
    need to reason about which candidate actually caused the incident.
    This module only produces that result; it does not call or depend on
    a Remediator."""
    return {
        "detector": "probe-detector",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "service_dependency_graph": {"edges": load_graph_edges()},
        "anomalies": anomalies,
        "named_root_cause": None,  # out of scope for this stage -- see docstring
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="write the shareable result JSON to this path")
    args = ap.parse_args()

    anomalies = scan(verbose=True)
    result = build_result(anomalies)

    if not anomalies:
        print("No anomalies detected.")
    else:
        anomalies_sorted = sorted(anomalies, key=lambda a: -a["deviation_score"])
        print(f"{len(anomalies)} anomal{'y' if len(anomalies) == 1 else 'ies'} detected:\n")
        for a in anomalies_sorted:
            print(f"  z={a['deviation_score']:6.2f}  {a['service']:18s} {a['signal_type']:20s} "
                  f"baseline={a['baseline']}  current={a['current']}  @{a['window']}")
        print()
        print(json.dumps(anomalies_sorted, indent=2))

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))
        print(f"\nWrote shareable result to {args.out}")


if __name__ == "__main__":
    main()
