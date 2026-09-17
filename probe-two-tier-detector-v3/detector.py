"""The Detector, v3 -- same two-tier shape as ../probe-two-tier-detector/
(spec: one module, two tiers, one output, no ranking/graph/root-cause),
with Tier 1 rebuilt per ../FIX-tier1-zscore.md (v2) and Tier 2's caller
now updated per ../FIX-tier2-changepoint.md:

  - a candidate's `reason` (e.g. "insufficient_data") is carried through
    from change_point() so a tier:1 candidate says *why* it didn't
    confirm, instead of just silently staying tier 1 (C1).
  - candidates are grouped by signal before Tier 2 runs; a group of more
    than 3 same-signal candidates goes through change_point.change_point_batch()
    (one query, BY service.name) instead of one change_point() call per
    candidate (C4). Groups of 3 or fewer still call change_point() per pair.

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
from collections import defaultdict
from datetime import datetime, timezone

from change_point import change_point, change_point_batch
from zscore_scan import zscore_scan

BATCH_THRESHOLD = 3  # C4: more than this many same-signal candidates -> one batched query


def _confirm_candidate(c: dict, cp: dict | None) -> dict:
    is_confirmed = bool(cp and cp.get("type") is not None)
    return {
        "service": c["service"],
        "signal": c["signal"],
        "type": cp["type"] if cp else None,
        "timestamp": (cp["timestamp"] if (cp and cp.get("timestamp")) else c["timestamp"]),
        "pvalue": cp["pvalue"] if cp else None,
        "breaks": cp.get("breaks") if cp else None,
        "reason": cp.get("reason") if cp else None,  # e.g. "insufficient_data" -- C1
        "z": c["z"],
        "streak": c["streak"],
        "recent_count": c["recent_count"],
        "tier": 2 if is_confirmed else 1,
    }


def detector_scan(streak_state: dict, cp_lookback: int | None = None, cp_bucket: int | None = None,
                   verbose: bool = False, **zscore_kwargs) -> dict | None:
    candidates = zscore_scan(streak_state, verbose=verbose, **zscore_kwargs)
    if not candidates:
        return None

    by_signal = defaultdict(list)
    for c in candidates:
        by_signal[c["signal"]].append(c)

    confirmed = []
    for signal, group in by_signal.items():  # every candidate, not just the loudest -- spec §2
        if len(group) > BATCH_THRESHOLD:  # C4: cascade -- one query instead of one per candidate
            services = [c["service"] for c in group]
            cp_by_service = change_point_batch(services, signal, cp_lookback, cp_bucket, verbose=verbose)
            for c in group:
                confirmed.append(_confirm_candidate(c, cp_by_service.get(c["service"])))
        else:
            for c in group:
                cp = change_point(c["service"], c["signal"], cp_lookback, cp_bucket, verbose=verbose)
                confirmed.append(_confirm_candidate(c, cp))

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
        reason_suffix = f"  reason={c['reason']}" if c.get("reason") else ""
        print(f"  tier={c['tier']}  {c['service']:18s} {c['signal']:16s} "
              f"z={c['z']:6.2f}  streak={c['streak']}  recent_count={c['recent_count']}  "
              f"type={c['type']}  pvalue={c['pvalue']}  @{c['timestamp']}{reason_suffix}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cp-lookback", type=int, default=None,
                     help="Tier 2 lookback, minutes -- overrides every signal's own default "
                          "(change_point._SIGNAL_TIMING) if set")
    ap.add_argument("--cp-bucket", type=int, default=None,
                     help="Tier 2 bucket size, seconds -- overrides every signal's own default if set")
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
