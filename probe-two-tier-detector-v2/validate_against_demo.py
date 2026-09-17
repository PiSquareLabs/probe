"""Validate the v2 detector per FIX-tier1-zscore.md §6 -- the only
validator change the fix doc asks for, distinct from
../probe-two-tier-detector/validate_against_demo.py's single-cycle battery:

- 5 cycles per flag, not 1
- 3 null windows (no fault injected) folded into the same battery
- score two ways: any-tier detected, tier-2-confirmed -- report both
- break misses down by signal type (latency vs. error vs. resource)
- report false shouts on null windows as a separate number

One streak_state dict is kept for the whole battery (not reset between
cycles/flags) -- this is the realistic shape: a long-running detector
process scanning every POLL_INTERVAL seconds, which is exactly what makes
persistence (C3) meaningful. Resetting state per-cycle would make
PERSISTENCE=2 impossible to ever satisfy on a freshly-injected fault
within one settle+poll window.

Usage:
    python validate_against_demo.py                  # full battery
    python validate_against_demo.py --only adHighCpu  # one flag, 5 cycles
    python validate_against_demo.py --cycles 3 --nulls 2
"""
import argparse
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from detector import detector_scan

REPO_ROOT = Path(__file__).resolve().parent.parent
FLAGD_PATH = REPO_ROOT / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"

FAULTS = {
    "adFailure": ("on", "ad", "error_rate"),
    "adHighCpu": ("on", "ad", "cpu"),
    "adManualGc": ("on", "ad", "p95_latency"),
    "cartFailure": ("100%", "cart", "error_rate"),
    "paymentFailure": ("100%", "payment", "error_rate"),
    "recommendationCacheFailure": ("on", "recommendation", "p95_latency"),
    "imageSlowLoad": ("10sec", "frontend", "p95_latency"),
    "intlShippingSlowdown": ("10sec", "shipping", "p95_latency"),
    "productCatalogFailure": ("on", "product-catalog", "error_rate"),
    "emailMemoryLeak": ("10x", "email", "memory"),
    "kafkaQueueProblems": ("on", None, None),
}

POLL_INTERVAL = 12
MAX_WAIT = 150  # a bit more headroom than v1's 120s: C3 costs one extra scan interval


def set_flag(name: str, variant: str):
    data = json.loads(FLAGD_PATH.read_text())
    data["flags"][name]["defaultVariant"] = variant
    FLAGD_PATH.write_text(json.dumps(data, indent=2))


def reset_flag(name: str):
    set_flag(name, "off")


def wait_for_detection(target_service: str, injected_at: float, streak_state: dict):
    deadline = injected_at + MAX_WAIT
    last_result = None
    while time.time() < deadline:
        result = detector_scan(streak_state)
        last_result = result
        if result:
            for c in result["candidates"]:
                if c["service"] == target_service and c["tier"] == 2:
                    return True, True, time.time() - injected_at, result
        time.sleep(POLL_INTERVAL)
    if last_result:
        for c in last_result["candidates"]:
            if c["service"] == target_service:
                return True, c["tier"] == 2, None, last_result
    return False, False, None, last_result


def run_null_window(streak_state: dict, seconds: int):
    """No fault injected. Any candidate that shouts here is a false shout."""
    deadline = time.time() + seconds
    shouts = []
    while time.time() < deadline:
        result = detector_scan(streak_state)
        if result:
            for c in result["candidates"]:
                shouts.append({"service": c["service"], "signal": c["signal"],
                                "tier": c["tier"], "z": c["z"]})
        time.sleep(POLL_INTERVAL)
    return shouts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--settle", type=int, default=15)
    ap.add_argument("--cycles", type=int, default=5, help="cycles per flag (fix doc §6: 5)")
    ap.add_argument("--nulls", type=int, default=3, help="null windows (fix doc §6: 3)")
    ap.add_argument("--null-seconds", type=int, default=90)
    args = ap.parse_args()

    flags_to_test = args.only or list(FAULTS.keys())
    streak_state = {}
    cycle_results = []
    false_shouts = []

    print(f"Testing {len(flags_to_test)} flags x {args.cycles} cycles, "
          f"plus {args.nulls} null windows. All flags start 'off'.\n")

    for flag in flags_to_test:
        variant, target, signal = FAULTS[flag]
        if target is None:
            print(f"=== {flag}: skipped, no detectable target service ===\n")
            for cycle in range(args.cycles):
                cycle_results.append({"flag": flag, "cycle": cycle, "target": None,
                                       "signal": None, "detected": False,
                                       "confirmed": False, "seconds": None})
            continue

        for cycle in range(args.cycles):
            print(f"=== {flag} cycle {cycle + 1}/{args.cycles} "
                  f"(variant={variant}, target={target}, signal={signal}) ===")
            set_flag(flag, variant)
            injected_at = time.time()
            print(f"  injected at {datetime.now(timezone.utc).isoformat()}, "
                  f"waiting {args.settle}s before first scan...")
            time.sleep(args.settle)

            detected, confirmed, seconds, result = wait_for_detection(
                target, injected_at, streak_state)
            reset_flag(flag)

            if confirmed:
                print(f"  CONFIRMED (tier 2) after {seconds:.1f}s")
            elif detected:
                print("  detected but not tier-2-confirmed"
                      + (f" after {seconds:.1f}s" if seconds else ""))
            else:
                others = sorted({c["service"] for c in result["candidates"]}) if result else []
                print(f"  MISSED within {MAX_WAIT}s (other candidates: {others or 'none'})")

            cycle_results.append({
                "flag": flag, "cycle": cycle, "target": target, "signal": signal,
                "detected": detected, "confirmed": confirmed,
                "seconds": round(seconds, 1) if seconds else None,
            })
            print()

    for n in range(args.nulls):
        print(f"=== null window {n + 1}/{args.nulls} ({args.null_seconds}s, no fault) ===")
        shouts = run_null_window(streak_state, args.null_seconds)
        false_shouts.extend(shouts)
        print(f"  {len(shouts)} shout(s) with nothing injected" if shouts else "  clean\n")

    # --- scoring ---
    by_flag = defaultdict(list)
    for r in cycle_results:
        by_flag[r["flag"]].append(r)

    detected_flags = sum(1 for flag, rs in by_flag.items() if any(r["detected"] for r in rs))
    confirmed_flags = sum(1 for flag, rs in by_flag.items() if any(r["confirmed"] for r in rs))
    total_flags = len(by_flag)

    per_signal = defaultdict(lambda: {"hit": 0, "miss": 0})
    for flag, rs in by_flag.items():
        signal = FAULTS[flag][2]
        if signal is None:
            continue
        hit = any(r["confirmed"] for r in rs)
        per_signal[signal]["hit" if hit else "miss"] += 1

    print("=" * 70)
    print(f"Detected (any tier), per-flag (any of {args.cycles} cycles hit): "
          f"{detected_flags}/{total_flags}")
    print(f"Tier-2-confirmed, per-flag: {confirmed_flags}/{total_flags}")
    print(f"False shouts across {args.nulls} null windows: {len(false_shouts)}")
    print("=" * 70)

    print("\nPer-signal miss table (tier-2-confirmed, at least one cycle):")
    for signal, counts in sorted(per_signal.items()):
        total = counts["hit"] + counts["miss"]
        print(f"  {signal:16s} {counts['hit']}/{total} confirmed")

    print("\nPer-flag detail (cycles that hit / total cycles):")
    for flag, rs in by_flag.items():
        detected_n = sum(1 for r in rs if r["detected"])
        confirmed_n = sum(1 for r in rs if r["confirmed"])
        best_seconds = min((r["seconds"] for r in rs if r["seconds"] is not None), default=None)
        tag = "CONFIRMED" if confirmed_n else ("detected" if detected_n else "MISSED")
        suffix = f" best={best_seconds}s" if best_seconds else ""
        print(f"  {flag:28s} {tag:10s} detected={detected_n}/{len(rs)} "
              f"confirmed={confirmed_n}/{len(rs)}{suffix}")

    if false_shouts:
        print("\nFalse shouts (null windows):")
        for s in false_shouts:
            print(f"  {s['service']:18s} {s['signal']:16s} tier={s['tier']} z={s['z']}")

    Path("validation_results.json").write_text(json.dumps({
        "cycles": cycle_results,
        "false_shouts": false_shouts,
        "summary": {
            "detected_any_per_flag": f"{detected_flags}/{total_flags}",
            "confirmed_t2_per_flag": f"{confirmed_flags}/{total_flags}",
            "false_shouts_per_nulls": f"{len(false_shouts)}/{args.nulls}",
            "per_signal": {k: v for k, v in per_signal.items()},
        },
    }, indent=2, default=str))
    print("\nWrote validation_results.json")


if __name__ == "__main__":
    main()
