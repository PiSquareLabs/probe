"""Validate the Detector against the Elastic OpenTelemetry demo by injecting
each known flagd fault one at a time and measuring whether/how fast the
Detector's z-score scan flags an anomaly in that fault's own target
service.

This measures the Detector stage alone (per ../idea/probe.pdf's
architecture), not the full PROBE pipeline -- it does not attempt to name a
single root cause among multiple candidates (that's the Correlator's job,
prototyped heuristically in ../causal-changepoint-detection/
changepoint_detector.py). The question this answers is narrower and
answerable without an LLM: "does *something* in the anomaly list point at
the right service, and how long does that take?"

Usage:
    python validate_against_demo.py --cycles 1
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from detector import scan

REPO_ROOT = Path(__file__).resolve().parent.parent
FLAGD_PATH = REPO_ROOT / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"

# flag -> ("on" variant, service the fault should make anomalous)
# Variant choices match ../ml-flag-detection/collect_dataset.py for
# consistency across this machine's detectors.
FAULTS = {
    "adFailure": ("on", "ad"),
    "adHighCpu": ("on", "ad"),
    "adManualGc": ("on", "ad"),
    "cartFailure": ("100%", "cart"),
    "paymentFailure": ("100%", "payment"),
    "recommendationCacheFailure": ("on", "recommendation"),
    "imageSlowLoad": ("10sec", "frontend"),  # image-provider itself is unaffected -- see README
    "intlShippingSlowdown": ("10sec", "shipping"),
    "productCatalogFailure": ("on", "product-catalog"),
    "emailMemoryLeak": ("10x", "email"),
    # kafkaQueueProblems has no direct trace/metric target service in this
    # detector's signal set (Kafka itself isn't traced) -- included with
    # target=None so it's honestly reported as "not detectable by this
    # Detector's current signals" rather than silently skipped.
    "kafkaQueueProblems": ("on", None),
}

POLL_INTERVAL = 10   # seconds between scan attempts after injection
MAX_WAIT = 120        # give up after this long


def set_flag(name: str, variant: str):
    data = json.loads(FLAGD_PATH.read_text())
    data["flags"][name]["defaultVariant"] = variant
    FLAGD_PATH.write_text(json.dumps(data, indent=2))


def reset_flag(name: str):
    set_flag(name, "off")


def wait_for_detection(target_service: str, injected_at: float):
    """Poll scan() until target_service shows up as anomalous or MAX_WAIT
    elapses. Returns (detected: bool, seconds: float|None, anomalies_seen)."""
    deadline = injected_at + MAX_WAIT
    last_anomalies = []
    while time.time() < deadline:
        anomalies = scan()
        last_anomalies = anomalies
        if any(a["service"] == target_service for a in anomalies):
            return True, time.time() - injected_at, anomalies
        time.sleep(POLL_INTERVAL)
    return False, None, last_anomalies


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="run only these flags (default: all)")
    ap.add_argument("--settle", type=int, default=15, help="seconds after injection before first scan")
    args = ap.parse_args()

    flags_to_test = args.only or list(FAULTS.keys())
    results = []

    print(f"Testing {len(flags_to_test)} flags. All flags start 'off'.\n")

    for flag in flags_to_test:
        variant, target = FAULTS[flag]
        print(f"=== {flag} (variant={variant}, target={target or 'N/A -- no direct signal'}) ===")

        if target is None:
            print("  skipped: no detectable target service for this flag's fault type\n")
            results.append({"flag": flag, "target": None, "detected": False,
                             "seconds": None, "note": "no direct trace/metric target"})
            continue

        set_flag(flag, variant)
        injected_at = time.time()
        print(f"  injected at {datetime.now(timezone.utc).isoformat()}, "
              f"waiting {args.settle}s before first scan...")
        time.sleep(args.settle)

        detected, seconds, anomalies = wait_for_detection(target, injected_at)
        reset_flag(flag)

        if detected:
            print(f"  DETECTED after {seconds:.1f}s")
        else:
            others = sorted({a["service"] for a in anomalies})
            print(f"  NOT detected within {MAX_WAIT}s "
                  f"(other anomalous services seen: {others or 'none'})")
        results.append({
            "flag": flag, "target": target, "detected": detected,
            "seconds": round(seconds, 1) if seconds else None,
            "other_anomalies": sorted({a["service"] for a in anomalies}) if not detected else None,
        })
        print()

    detected_count = sum(1 for r in results if r["detected"])
    total = len(results)
    times = [r["seconds"] for r in results if r["detected"]]
    mean_time = sum(times) / len(times) if times else None

    print("=" * 60)
    print(f"Detected {detected_count}/{total} injected faults"
          + (f" at a mean of {mean_time:.1f}s" if mean_time else ""))
    print("=" * 60)
    for r in results:
        status = f"{r['seconds']}s" if r["detected"] else "MISSED"
        print(f"  {r['flag']:28s} target={str(r['target']):18s} {status}")

    Path("validation_results.json").write_text(json.dumps(results, indent=2))
    print("\nWrote validation_results.json")


if __name__ == "__main__":
    main()
