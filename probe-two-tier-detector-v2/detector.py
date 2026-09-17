"""The Detector, v2 -- same two-tier shape as ../probe-two-tier-detector/
(spec: one module, two tiers, one output, no ranking/graph/root-cause),
with Tier 1 rebuilt per ../FIX-tier1-zscore.md. Tier 2 (change_point.py)
is untouched, copied verbatim, per that doc's explicit scope statement.

The one real interface change: Tier 1's persistence (C3) needs a
streak counter that survives across scans. A fresh `python detector.py`
process has no memory of the previous scan, so PERSISTENCE=2 would never
fire in one-shot mode -- the first qualifying scan always resets state.
Two ways to use this honestly:

    python detector.py                 # one-shot; streak starts at 0 each
                                        # time, so PERSISTENCE effectively
                                        # requires 2 *manual* re-runs
    python detector.py --loop 10       # long-running; keeps streak_state
                                        # across iterations, so persistence
                                        # behaves as FIX-tier1-zscore.md
                                        # actually describes it

validate_against_demo.py uses the library function detector_scan()
directly in its own polling loop and keeps one streak_state dict for the
whole battery, which is the realistic deployment shape (a supervisor
process scanning every N seconds) -- see that file.
"""
import argparse
import json
import time
from datetime import datetime, timezone

from change_point import change_point
from zscore_scan import zscore_scan


def detector_scan(streak_state: dict, cp_lookback: int = 5, cp_bucket: int = 2,
                   verbose: bool = False, **zscore_kwargs) -> dict | None:
    candidates = zscore_scan(streak_state, verbose=verbose, **zscore_kwargs)
    if not candidates:
        return None

    confirmed = []
    for c in candidates:  # every candidate, not just the loudest -- spec §2
        cp = change_point(c["service"], c["signal"], cp_lookback, cp_bucket, verbose=verbose)
        confirmed.append({
            "service": c["service"],
            "signal": c["signal"],
            "type": cp["type"] if cp else None,
            "timestamp": cp["timestamp"] if cp else c["timestamp"],
            "pvalue": cp["pvalue"] if cp else None,
            "z": c["z"],
            "streak": c["streak"],
            "recent_count": c["recent_count"],
            "tier": 2 if cp else 1,
        })

    tier2 = [c for c in confirmed if c["tier"] == 2]
    return {
        "candidates": confirmed,
        "loudest": max(candidates, key=lambda c: c["z"])["service"],
        "earliest": (min(tier2, key=lambda c: c["timestamp"])["service"]
                     if tier2 else confirmed[0]["service"]),
        "scan_at": datetime.now(timezone.utc).isoformat(),
    }


def _print_result(result: dict | None):
    if result is None:
        print("No candidates. System looks stable.")
        return
    print(f"{len(result['candidates'])} candidate(s), "
          f"loudest={result['loudest']}, earliest={result['earliest']}\n")
    for c in result["candidates"]:
        print(f"  tier={c['tier']}  {c['service']:18s} {c['signal']:16s} "
              f"z={c['z']:6.2f}  streak={c['streak']}  recent_count={c['recent_count']}  "
              f"type={c['type']}  pvalue={c['pvalue']}  @{c['timestamp']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cp-lookback", type=int, default=5, help="Tier 2 lookback, minutes")
    ap.add_argument("--cp-bucket", type=int, default=2, help="Tier 2 bucket size, seconds")
    ap.add_argument("--loop", type=int, default=None, metavar="SECONDS",
                     help="keep scanning every SECONDS, carrying streak state "
                          "across iterations -- needed for persistence (C3) "
                          "to mean anything (see module docstring)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    streak_state = {}

    if args.loop is None:
        result = detector_scan(streak_state, args.cp_lookback, args.cp_bucket, args.verbose)
        _print_result(result)
        if args.out:
            with open(args.out, "w") as f:
                json.dump(result, f, indent=2)
            print(f"\nWrote result to {args.out}")
        return

    print(f"Looping every {args.loop}s (Ctrl+C to stop)...\n")
    try:
        while True:
            result = detector_scan(streak_state, args.cp_lookback, args.cp_bucket, args.verbose)
            print(f"--- scan at {datetime.now(timezone.utc).isoformat()} ---")
            _print_result(result)
            print()
            if args.out and result:
                with open(args.out, "w") as f:
                    json.dump(result, f, indent=2)
            time.sleep(args.loop)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
