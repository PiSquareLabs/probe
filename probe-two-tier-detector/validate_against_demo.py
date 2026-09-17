"""Validate the two-tier detector against the same 11 known flagd faults
used by every other detector in this repo, for a like-for-like comparison.

Two success bars, since the spec's whole point is that Tier 1 shouting
isn't enough on its own:
  - "detected": the fault's target service appears anywhere in
    result["candidates"] (tier 1 or 2)
  - "confirmed": it appears with tier == 2 (CHANGE_POINT actually found a
    statistically significant break, not just an elevated z-score)

Usage:
    python validate_against_demo.py --settle 15
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from detector import detector_scan

REPO_ROOT = Path(__file__).resolve().parent.parent
FLAGD_PATH = REPO_ROOT / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"

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

POLL_INTERVAL = 12
MAX_WAIT = 120


def set_flag(name: str, variant: str):
    data = json.loads(FLAGD_PATH.read_text())
    data["flags"][name]["defaultVariant"] = variant
    FLAGD_PATH.write_text(json.dumps(data, indent=2))


def reset_flag(name: str):
    set_flag(name, "off")


def wait_for_detection(target_service: str, injected_at: float):
    deadline = injected_at + MAX_WAIT
    last_result = None
    while time.time() < deadline:
        result = detector_scan()
        last_result = result
        if result:
            for c in result["candidates"]:
                if c["service"] == target_service and c["tier"] == 2:
                    return True, True, time.time() - injected_at, result
                # tier 1 only: keep polling in case a later scan's CHANGE_POINT
                # confirms it -- don't return early on partial detection
        time.sleep(POLL_INTERVAL)
    if last_result:
        for c in last_result["candidates"]:
            if c["service"] == target_service:
                return True, c["tier"] == 2, None, last_result
    return False, False, None, last_result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--settle", type=int, default=15)
    args = ap.parse_args()

    flags_to_test = args.only or list(FAULTS.keys())
    results = []

    print(f"Testing {len(flags_to_test)} flags. All flags start 'off'.\n")

    for flag in flags_to_test:
        variant, target = FAULTS[flag]
        print(f"=== {flag} (variant={variant}, target={target or 'N/A'}) ===")

        if target is None:
            print("  skipped: no detectable target service\n")
            results.append({"flag": flag, "target": None, "detected": False,
                             "confirmed": False, "seconds": None})
            continue

        set_flag(flag, variant)
        injected_at = time.time()
        print(f"  injected at {datetime.now(timezone.utc).isoformat()}, "
              f"waiting {args.settle}s before first scan...")
        time.sleep(args.settle)

        detected, confirmed, seconds, result = wait_for_detection(target, injected_at)
        reset_flag(flag)

        if confirmed:
            print(f"  CONFIRMED (tier 2) after {seconds:.1f}s")
        elif detected:
            print(f"  detected but not tier-2-confirmed"
                  + (f" after {seconds:.1f}s" if seconds else ""))
        else:
            others = sorted({c["service"] for c in result["candidates"]}) if result else []
            print(f"  MISSED within {MAX_WAIT}s (other candidates: {others or 'none'})")

        results.append({
            "flag": flag, "target": target, "detected": detected,
            "confirmed": confirmed, "seconds": round(seconds, 1) if seconds else None,
        })
        print()

    detected_count = sum(1 for r in results if r["detected"])
    confirmed_count = sum(1 for r in results if r["confirmed"])
    total = len(results)

    print("=" * 60)
    print(f"Detected (any tier): {detected_count}/{total}")
    print(f"Tier-2-confirmed: {confirmed_count}/{total}")
    print("=" * 60)
    for r in results:
        tag = "CONFIRMED" if r["confirmed"] else ("detected" if r["detected"] else "MISSED")
        print(f"  {r['flag']:28s} target={str(r['target']):18s} {tag:10s} {r['seconds']}s")

    Path("validation_results.json").write_text(json.dumps(results, indent=2))
    print("\nWrote validation_results.json")


if __name__ == "__main__":
    main()
