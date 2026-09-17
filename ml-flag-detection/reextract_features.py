"""
Re-run feature extraction against Elasticsearch for the time windows already
recorded in dataset.csv, using a corrected trace query that excludes flagd's
own internal streaming/evaluation RPC spans (flagd.evaluation.v1.Service/*).

Those spans reconnect/error constantly as background noise unrelated to any
injected fault, and were inflating every service's "throughput" and diluting
its error_rate in the first extraction pass. This does not need the demo
stack to still be emitting traffic -- it only re-reads what's already stored
in Elasticsearch for the windows we already sampled.

Targets the local self-hosted Elasticsearch by default; override with
ES_URL + ES_API_KEY (or ES_USERNAME/ES_PASSWORD) to point at Elastic
Cloud/Serverless instead -- see ../RUNNING_ON_ELASTIC_CLOUD.md.
"""
import base64
import csv
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START_LOCAL_ENV = REPO_ROOT / "opentelemetry-demo" / "elastic-start-local" / ".env"
ES_URL = os.environ.get("ES_URL", "http://localhost:9200")

SERVICES = [
    "ad", "cart", "checkout", "currency", "email", "frontend", "frontend-proxy",
    "image-provider", "payment", "product-catalog", "quote", "recommendation",
    "shipping", "flagd", "flagd-ui", "kafka", "accounting", "fraud-detection",
]


def es_password():
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
    password = os.environ.get("ES_PASSWORD") or es_password()
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


AUTH_HEADER = _auth_header()


def es_search(index, body):
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_search?ignore_unavailable=true",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": AUTH_HEADER},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def query_trace_features(start_iso, end_iso):
    body = {
        "size": 0,
        "query": {"bool": {
            "filter": [
                {"range": {"@timestamp": {"gte": start_iso, "lt": end_iso}}},
                {"term": {"attributes.processor.event": "transaction"}},
            ],
            # exclude flagd's own client-library streaming/evaluation RPCs --
            # constant background reconnects that are not application traffic
            "must_not": [{"wildcard": {"name": "flagd.evaluation.v1.Service*"}}],
        }},
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
    from datetime import datetime
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
                # Node.js / V8 heap metrics (email, frontend, checkout-ui etc.)
                "v8_mem_avg": {"avg": {"field": "metrics.v8js.memory.heap.used"}},
                "v8_mem_max": {"max": {"field": "metrics.v8js.memory.heap.used"}},
                # .NET metrics (fraud-detection)
                "dotnet_mem_avg": {"avg": {"field": "metrics.dotnet.process.memory.working_set"}},
                "dotnet_cpu_avg": {"avg": {"field": "metrics.dotnet.process.cpu.time"}},
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

        cpu = val("cpu_avg") or val("jvm_cpu_avg") or val("cpython_cpu_avg") or val("dotnet_cpu_avg")
        cpu_max = val("cpu_max") or val("jvm_cpu_max")
        mem = val("mem_avg") or val("jvm_mem_avg") or val("go_mem_avg") or val("cpython_mem_avg") or val("v8_mem_avg") or val("dotnet_mem_avg")
        mem_max = val("mem_max") or val("jvm_mem_max") or val("go_mem_max") or val("v8_mem_max")
        out[svc] = {
            "cpu_avg": cpu, "cpu_max": cpu_max,
            "mem_avg_mb": mem / (1024 * 1024) if mem else 0.0,
            "mem_max_mb": mem_max / (1024 * 1024) if mem_max else 0.0,
        }
    return out


def flatten_window(label, trace_feats, resource_feats, window_start, window_end):
    row = {"label": label, "window_start": window_start, "window_end": window_end}
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


def main():
    out_path = Path("dataset_v2.csv")
    with open("dataset.csv", newline="") as f:
        src_rows = list(csv.DictReader(f))

    fieldnames = None
    done = 0
    if out_path.exists():
        with open(out_path, newline="") as f:
            existing = list(csv.DictReader(f))
        done = len(existing)
        if existing:
            fieldnames = list(existing[0].keys())

    write_header = not out_path.exists()
    with open(out_path, "a", newline="") as out_f:
        writer = None
        for i, r in enumerate(src_rows):
            if i < done:
                continue
            t = query_trace_features(r["window_start"], r["window_end"])
            res = query_resource_features(r["window_start"], r["window_end"])
            row = flatten_window(r["label"], t, res, r["window_start"], r["window_end"])
            if writer is None:
                fieldnames = list(row.keys())
                writer = csv.DictWriter(out_f, fieldnames=fieldnames)
                if write_header:
                    writer.writeheader()
            writer.writerow(row)
            out_f.flush()
            print(f"{i + 1}/{len(src_rows)} {r['label']}", flush=True)
    print(f"Done: {len(src_rows)} rows total in dataset_v2.csv")


if __name__ == "__main__":
    main()
