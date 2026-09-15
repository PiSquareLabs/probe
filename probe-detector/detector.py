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
import json
import logging

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


def main():
    anomalies = scan(verbose=True)
    if not anomalies:
        print("No anomalies detected.")
        return
    anomalies.sort(key=lambda a: -a["deviation_score"])
    print(f"{len(anomalies)} anomal{'y' if len(anomalies) == 1 else 'ies'} detected:\n")
    for a in anomalies:
        print(f"  z={a['deviation_score']:6.2f}  {a['service']:18s} {a['signal_type']:20s} "
              f"baseline={a['baseline']}  current={a['current']}  @{a['window']}")
    print()
    print(json.dumps(anomalies, indent=2))


if __name__ == "__main__":
    main()
