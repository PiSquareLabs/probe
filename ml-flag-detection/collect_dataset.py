"""
Toggle each flagd feature flag on/off against the Elastic OpenTelemetry demo
stack (es-flagtest-* containers) and record aggregated per-service telemetry
windows from Elasticsearch, labeled by which flag was active.

Usage:
    python collect_dataset.py --out dataset.csv
"""
import argparse
import json
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import requests

warnings.filterwarnings("ignore")

REPO_ROOT = Path(__file__).resolve().parent.parent
FLAGD_PATH = REPO_ROOT / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"
START_LOCAL_ENV = REPO_ROOT / "opentelemetry-demo" / "elastic-start-local" / ".env"

ES_URL = "http://localhost:9200"

# Requested flags -> the variant that switches the failure/behavior "on".
# NOTE: productCatalogLockContention does not exist in this fork's
# src/flagd/demo.flagd.json (verified against the checked-out file); it is
# skipped and called out in the summary.
FLAG_VARIANTS = {
    "adFailure": "on",
    "adHighCpu": "on",
    "adManualGc": "on",
    "cartFailure": "100%",
    "paymentFailure": "100%",
    "recommendationCacheFailure": "on",
    "kafkaQueueProblems": "on",
    "imageSlowLoad": "10sec",
    "intlShippingSlowdown": "10sec",
    "productCatalogFailure": "on",
    "emailMemoryLeak": "10x",
}

SERVICES = [
    "ad", "cart", "checkout", "currency", "email", "frontend", "frontend-proxy",
    "image-provider", "payment", "product-catalog", "quote", "recommendation",
    "shipping", "flagd", "flagd-ui", "kafka", "accounting", "fraud-detection",
]

CYCLE_DURATION = 40   # seconds of load to observe per window
SETTLE_BEFORE = 10    # seconds to let the flag change propagate before sampling
SETTLE_AFTER = 10     # seconds to let the system recover after reverting


def es_password():
    for line in START_LOCAL_ENV.read_text().splitlines():
        if line.startswith("ES_LOCAL_PASSWORD="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("ES_LOCAL_PASSWORD not found in elastic-start-local/.env")


AUTH = ("elastic", es_password())


def set_flag(name: str, variant: str):
    data = json.loads(FLAGD_PATH.read_text())
    data["flags"][name]["defaultVariant"] = variant
    FLAGD_PATH.write_text(json.dumps(data, indent=2))


def reset_all_flags():
    data = json.loads(FLAGD_PATH.read_text())
    for name in FLAG_VARIANTS:
        data["flags"][name]["defaultVariant"] = "off"
    FLAGD_PATH.write_text(json.dumps(data, indent=2))


def es_search(index: str, body: dict) -> dict:
    resp = requests.get(
        f"{ES_URL}/{index}/_search",
        auth=AUTH, json=body, timeout=30,
        params={"ignore_unavailable": "true"},
    )
    resp.raise_for_status()
    return resp.json()


def query_trace_features(start_iso: str, end_iso: str) -> dict:
    body = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"range": {"@timestamp": {"gte": start_iso, "lt": end_iso}}},
                    {"term": {"attributes.processor.event": "transaction"}},
                ]
            }
        },
        "aggs": {
            "by_service": {
                "terms": {"field": "resource.attributes.service.name", "size": 50},
                "aggs": {
                    "p": {"percentiles": {"field": "duration", "percents": [50, 95, 99]}},
                    "failures": {"filter": {"term": {"attributes.event.outcome": "failure"}}},
                },
            }
        },
    }
    result = es_search("traces-*,.ds-traces-*", body)
    out = {}
    window_seconds = (
        datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
    ).total_seconds()
    for bucket in result.get("aggregations", {}).get("by_service", {}).get("buckets", []):
        svc = bucket["key"]
        total = bucket["doc_count"]
        fail = bucket["failures"]["doc_count"]
        p = bucket["p"]["values"]
        out[svc] = {
            "p50_ms": (p.get("50.0") or 0) / 1e6,
            "p95_ms": (p.get("95.0") or 0) / 1e6,
            "p99_ms": (p.get("99.0") or 0) / 1e6,
            "error_rate": fail / total if total else 0.0,
            "throughput_rps": total / window_seconds if window_seconds else 0.0,
        }
    return out


def query_resource_features(start_iso: str, end_iso: str) -> dict:
    body = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [{"range": {"@timestamp": {"gte": start_iso, "lt": end_iso}}}]
            }
        },
        "aggs": {
            "by_service": {
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
            }
        },
    }
    result = es_search("metrics-*,.ds-metrics-*", body)
    out = {}
    for bucket in result.get("aggregations", {}).get("by_service", {}).get("buckets", []):
        svc = bucket["key"]

        def val(agg_name):
            v = bucket[agg_name]["value"]
            return v if v is not None else 0.0

        # Prefer the language-neutral process.* metrics; fall back to JVM ones
        # (jvm.memory.used is bytes just like process.memory.usage).
        cpu = val("cpu_avg") or val("jvm_cpu_avg") or val("cpython_cpu_avg")
        cpu_max = val("cpu_max") or val("jvm_cpu_max")
        mem = val("mem_avg") or val("jvm_mem_avg") or val("go_mem_avg") or val("cpython_mem_avg")
        mem_max = val("mem_max") or val("jvm_mem_max") or val("go_mem_max")
        out[svc] = {
            "cpu_avg": cpu,
            "cpu_max": cpu_max,
            "mem_avg_mb": mem / (1024 * 1024) if mem else 0.0,
            "mem_max_mb": mem_max / (1024 * 1024) if mem_max else 0.0,
        }
    return out


def flatten_window(label: str, trace_feats: dict, resource_feats: dict) -> dict:
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


def run_window(label: str) -> dict:
    start = datetime.now(timezone.utc)
    time.sleep(CYCLE_DURATION)
    end = datetime.now(timezone.utc)
    # small buffer for the collector/ES ingest pipeline to catch up
    time.sleep(3)
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    trace_feats = query_trace_features(start_iso, end_iso)
    resource_feats = query_resource_features(start_iso, end_iso)
    row = flatten_window(label, trace_feats, resource_feats)
    row["window_start"] = start_iso
    row["window_end"] = end_iso
    return row


def main():
    global CYCLE_DURATION, SETTLE_BEFORE, SETTLE_AFTER
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dataset.csv")
    ap.add_argument("--cycles-per-flag", type=int, default=6)
    ap.add_argument("--baseline-cycles", type=int, default=12)
    ap.add_argument("--append", action="store_true", help="append to an existing CSV")
    ap.add_argument("--cycle-duration", type=int, default=CYCLE_DURATION)
    ap.add_argument("--settle-before", type=int, default=SETTLE_BEFORE)
    ap.add_argument("--settle-after", type=int, default=SETTLE_AFTER)
    args = ap.parse_args()

    CYCLE_DURATION = args.cycle_duration
    SETTLE_BEFORE = args.settle_before
    SETTLE_AFTER = args.settle_after

    import pandas as pd

    rows = []
    if args.append and Path(args.out).exists():
        rows = pd.read_csv(args.out).to_dict("records")
        done_baseline = sum(1 for r in rows if r["label"] == "baseline")
        done_flags = {f: sum(1 for r in rows if r["label"] == f) for f in FLAG_VARIANTS}
        print(f"Resuming from {args.out}: {len(rows)} existing rows "
              f"({done_baseline} baseline, {done_flags})", flush=True)
    else:
        done_baseline = 0
        done_flags = {f: 0 for f in FLAG_VARIANTS}

    def save():
        pd.DataFrame(rows).to_csv(args.out, index=False)

    def safe_window(label: str, retries: int = 2):
        for attempt in range(retries + 1):
            try:
                return run_window(label)
            except Exception as e:
                print(f"  window failed ({e}); "
                      f"{'retrying' if attempt < retries else 'giving up on this cycle'}", flush=True)
                time.sleep(5)
        return None

    if done_baseline == 0:
        reset_all_flags()
        print("All flags reset to off. Warming up for 15s...", flush=True)
        time.sleep(15)

    print(f"Collecting {args.baseline_cycles} baseline windows...", flush=True)
    for i in range(done_baseline, args.baseline_cycles):
        print(f"  baseline cycle {i + 1}/{args.baseline_cycles}", flush=True)
        row = safe_window("baseline")
        if row:
            rows.append(row)
            save()

    for flag, variant in FLAG_VARIANTS.items():
        start_i = done_flags.get(flag, 0)
        print(f"Collecting {args.cycles_per_flag} windows for flag={flag} (variant={variant})", flush=True)
        for i in range(start_i, args.cycles_per_flag):
            print(f"  {flag} cycle {i + 1}/{args.cycles_per_flag}: turning on", flush=True)
            set_flag(flag, variant)
            time.sleep(SETTLE_BEFORE)
            row = safe_window(flag)
            print(f"  {flag} cycle {i + 1}/{args.cycles_per_flag}: reverting to off", flush=True)
            set_flag(flag, "off")
            time.sleep(SETTLE_AFTER)
            if row:
                rows.append(row)
                save()

    save()
    print(f"Wrote {len(rows)} rows to {args.out}", flush=True)


if __name__ == "__main__":
    main()
