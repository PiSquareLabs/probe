"""
Lean, stdlib-only version of collect_dataset.py for finishing the last few
flags on a memory-starved host: no pandas/numpy/requests imports, just
urllib + json + csv, to minimize the process's own memory footprint.
"""
import csv
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FLAGD_PATH = REPO_ROOT / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"
START_LOCAL_ENV = REPO_ROOT / "opentelemetry-demo" / "elastic-start-local" / ".env"
ES_URL = "http://localhost:9200"

SERVICES = [
    "ad", "cart", "checkout", "currency", "email", "frontend", "frontend-proxy",
    "image-provider", "payment", "product-catalog", "quote", "recommendation",
    "shipping", "flagd", "flagd-ui", "kafka", "accounting", "fraud-detection",
]

CYCLE_DURATION = 25
SETTLE_BEFORE = 5
SETTLE_AFTER = 5


def es_password():
    for line in START_LOCAL_ENV.read_text().splitlines():
        if line.startswith("ES_LOCAL_PASSWORD="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("ES_LOCAL_PASSWORD not found")


import base64
AUTH_HEADER = "Basic " + base64.b64encode(f"elastic:{es_password()}".encode()).decode()


def es_search(index: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_search?ignore_unavailable=true",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": AUTH_HEADER},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def set_flag(name: str, variant: str):
    data = json.loads(FLAGD_PATH.read_text())
    data["flags"][name]["defaultVariant"] = variant
    FLAGD_PATH.write_text(json.dumps(data, indent=2))


def query_trace_features(start_iso, end_iso):
    body = {
        "size": 0,
        "query": {"bool": {"filter": [
            {"range": {"@timestamp": {"gte": start_iso, "lt": end_iso}}},
            {"term": {"attributes.processor.event": "transaction"}},
        ]}},
        "aggs": {"by_service": {
            "terms": {"field": "resource.attributes.service.name", "size": 50},
            "aggs": {
                "p": {"percentiles": {"field": "duration", "percents": [50, 95, 99]}},
                "failures": {"filter": {"term": {"attributes.event.outcome": "failure"}}},
            },
        }},
    }
    result = es_search("traces-*,.ds-traces-*", body)
    out = {}
    window_seconds = (datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)).total_seconds()
    for b in result.get("aggregations", {}).get("by_service", {}).get("buckets", []):
        svc = b["key"]
        total = b["doc_count"]
        fail = b["failures"]["doc_count"]
        p = b["p"]["values"]
        out[svc] = {
            "p50_ms": (p.get("50.0") or 0) / 1e6,
            "p95_ms": (p.get("95.0") or 0) / 1e6,
            "p99_ms": (p.get("99.0") or 0) / 1e6,
            "error_rate": fail / total if total else 0.0,
            "throughput_rps": total / window_seconds if window_seconds else 0.0,
        }
    return out


def query_resource_features(start_iso, end_iso):
    body = {
        "size": 0,
        "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": start_iso, "lt": end_iso}}}]}},
        "aggs": {"by_service": {
            "terms": {"field": "resource.attributes.service.name", "size": 50},
            "aggs": {
                "cpu_avg": {"avg": {"field": "metrics.process.cpu.utilization"}},
                "cpu_max": {"max": {"field": "metrics.process.cpu.utilization"}},
                "mem_avg": {"avg": {"field": "metrics.process.memory.usage"}},
                "mem_max": {"max": {"field": "metrics.process.memory.usage"}},
                "jvm_cpu_avg": {"avg": {"field": "metrics.jvm.cpu.recent_utilization"}},
                "jvm_cpu_max": {"max": {"field": "metrics.jvm.cpu.recent_utilization"}},
                "jvm_mem_avg": {"avg": {"field": "metrics.jvm.memory.used"}},
                "jvm_mem_max": {"max": {"field": "metrics.jvm.memory.used"}},
                "go_mem_avg": {"avg": {"field": "metrics.go.memory.used"}},
                "go_mem_max": {"max": {"field": "metrics.go.memory.used"}},
                "cpython_cpu_avg": {"avg": {"field": "metrics.process.runtime.cpython.cpu.utilization"}},
                "cpython_mem_avg": {"avg": {"field": "metrics.process.runtime.cpython.memory"}},
            },
        }},
    }
    result = es_search("metrics-*,.ds-metrics-*", body)
    out = {}
    for b in result.get("aggregations", {}).get("by_service", {}).get("buckets", []):
        svc = b["key"]

        def val(name):
            v = b[name]["value"]
            return v if v is not None else 0.0

        cpu = val("cpu_avg") or val("jvm_cpu_avg") or val("cpython_cpu_avg")
        cpu_max = val("cpu_max") or val("jvm_cpu_max")
        mem = val("mem_avg") or val("jvm_mem_avg") or val("go_mem_avg") or val("cpython_mem_avg")
        mem_max = val("mem_max") or val("jvm_mem_max") or val("go_mem_max")
        out[svc] = {
            "cpu_avg": cpu, "cpu_max": cpu_max,
            "mem_avg_mb": mem / (1024 * 1024) if mem else 0.0,
            "mem_max_mb": mem_max / (1024 * 1024) if mem_max else 0.0,
        }
    return out


def flatten_window(label, trace_feats, resource_feats):
    row = {"label": label}
    for svc in SERVICES:
        t = trace_feats.get(svc, {})
        r = resource_feats.get(svc, {})
        prefix = svc.replace("-", "_")
        row[f"{prefix}_p50_ms"] = t.get("p50_ms", 0.0)
        row[f"{prefix}_p95_ms"] = t.get("p95_ms", 0.0)
        row[f"{prefix}_p99_ms"] = t.get("p99_ms", 0.0)
        row[f"{prefix}_error_rate"] = t.get("error_rate", 0.0)
        row[f"{prefix}_throughput_rps"] = t.get("throughput_rps", 0.0)
        row[f"{prefix}_cpu_avg"] = r.get("cpu_avg", 0.0)
        row[f"{prefix}_cpu_max"] = r.get("cpu_max", 0.0)
        row[f"{prefix}_mem_avg_mb"] = r.get("mem_avg_mb", 0.0)
        row[f"{prefix}_mem_max_mb"] = r.get("mem_max_mb", 0.0)
    return row


def run_window(label):
    start = datetime.now(timezone.utc)
    time.sleep(CYCLE_DURATION)
    end = datetime.now(timezone.utc)
    time.sleep(3)
    start_iso, end_iso = start.isoformat(), end.isoformat()
    t = query_trace_features(start_iso, end_iso)
    r = query_resource_features(start_iso, end_iso)
    row = flatten_window(label, t, r)
    row["window_start"] = start_iso
    row["window_end"] = end_iso
    return row


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "dataset.csv"
    flag = sys.argv[2]
    variant = sys.argv[3]
    n_cycles = int(sys.argv[4]) if len(sys.argv) > 4 else 5

    with open(out_path, newline="") as f:
        header = next(csv.reader(f))

    for i in range(n_cycles):
        print(f"{flag} cycle {i + 1}/{n_cycles}: turning on", flush=True)
        set_flag(flag, variant)
        time.sleep(SETTLE_BEFORE)
        row = run_window(flag)
        print(f"{flag} cycle {i + 1}/{n_cycles}: reverting to off", flush=True)
        set_flag(flag, "off")
        time.sleep(SETTLE_AFTER)

        with open(out_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=header)
            writer.writerow({k: row.get(k, 0.0) for k in header})
        print(f"  saved row {i + 1}", flush=True)


if __name__ == "__main__":
    main()
