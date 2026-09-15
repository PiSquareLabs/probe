"""Validate the causal changepoint detector against the same 11 known
flagd faults used by ../probe-detector/validate_against_demo.py and
../ml-flag-detection/collect_dataset.py, for a fair three-way comparison.

Methodology differs slightly from probe-detector's validator out of
necessity: each full scan here runs CHANGE_POINT twice (latency + error
rate) for every one of 12 services -- 24 sequential ES|QL queries, each
taking 1-8s -- so continuous polling every 10-15s (as probe-detector does)
would be prohibitively slow. Instead this does one scan after a fixed
settle time per flag and reports:
  - "detected": is the target service anywhere in the candidate set
    (comparable to probe-detector's detection bar)
  - "named_root_cause": is the target service the #1 ranked candidate
    (the harder bar -- this is what ../causal-changepoint-detection/
    actually adds over a plain anomaly list: naming ONE cause)

Usage:
    python validate_against_demo.py --settle 60
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from changepoint_detector import find_candidates, load_graph, rank_candidates

REPO_ROOT = Path(__file__).resolve().parent.parent
FLAGD_PATH = REPO_ROOT / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"

# Same fault -> target-service mapping as ../probe-detector/validate_against_demo.py
FAULTS = {
    "adFailure": ("on", "ad"),
    "adHighCpu": ("on", "ad"),
    "adManualGc": ("on", "ad"),
    "cartFailure": ("100%", "cart"),
    "paymentFailure": ("100%", "payment"),
    "recommendationCacheFailure": ("on", "recommendation"),
    "imageSlowLoad": ("10sec", "frontend"),
    "intlShippingSlowdown": ("10sec", "shipping"),
    "productCatalogFailure": ("on", "product-catalog"),
    "emailMemoryLeak": ("10x", "email"),
    "kafkaQueueProblems": ("on", None),
}


def set_flag(name: str, variant: str):
    data = json.loads(FLAGD_PATH.read_text())
    data["flags"][name]["defaultVariant"] = variant
    FLAGD_PATH.write_text(json.dumps(data, indent=2))


def reset_flag(name: str):
    set_flag(name, "off")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--settle", type=int, default=60,
                     help="seconds after injection before running the scan")
    ap.add_argument("--lookback", type=int, default=15)
    ap.add_argument("--bucket", type=int, default=20)
    args = ap.parse_args()

    flags_to_test = args.only or list(FAULTS.keys())
    downstream = load_graph()
    results = []

    print(f"Testing {len(flags_to_test)} flags, settle={args.settle}s, "
          f"lookback={args.lookback}min, bucket={args.bucket}s.\n")

    for flag in flags_to_test:
        variant, target = FAULTS[flag]
        print(f"=== {flag} (variant={variant}, target={target or 'N/A'}) ===")

        if target is None:
            print("  skipped: no detectable target service\n")
            results.append({"flag": flag, "target": None, "detected": False,
                             "named_root_cause": False, "seconds": None})
            continue

        set_flag(flag, variant)
        injected_at = time.time()
        print(f"  injected at {datetime.now(timezone.utc).isoformat()}, "
              f"settling {args.settle}s...")
        time.sleep(args.settle)

        candidates = find_candidates(args.lookback, args.bucket)
        elapsed = time.time() - injected_at
        reset_flag(flag)

        detected = target in candidates
        ranked = rank_candidates(candidates, downstream) if candidates else []
        named = bool(ranked) and ranked[0]["service"] == target

        if named:
            print(f"  NAMED ROOT CAUSE after {elapsed:.1f}s "
                  f"(candidates: {sorted(candidates)})")
        elif detected:
            top = ranked[0]["service"] if ranked else "?"
            print(f"  detected but NOT top-ranked after {elapsed:.1f}s "
                  f"(top={top}, candidates: {sorted(candidates)})")
        else:
            print(f"  MISSED after {elapsed:.1f}s "
                  f"(candidates seen: {sorted(candidates) or 'none'})")
        results.append({
            "flag": flag, "target": target, "detected": detected,
            "named_root_cause": named,
            "seconds": round(elapsed, 1),
            "candidates": sorted(candidates),
            "top_ranked": ranked[0]["service"] if ranked else None,
        })
        print()

    detected_count = sum(1 for r in results if r["detected"])
    named_count = sum(1 for r in results if r["named_root_cause"])
    total = len(results)

    print("=" * 60)
    print(f"Detected (in candidate set): {detected_count}/{total}")
    print(f"Correctly named as #1 root cause: {named_count}/{total}")
    print("=" * 60)
    for r in results:
        tag = "NAMED" if r["named_root_cause"] else ("detected" if r["detected"] else "MISSED")
        print(f"  {r['flag']:28s} target={str(r['target']):18s} {tag:10s} {r['seconds']}s")

    Path("validation_results.json").write_text(json.dumps(results, indent=2))
    print("\nWrote validation_results.json")


if __name__ == "__main__":
    main()
