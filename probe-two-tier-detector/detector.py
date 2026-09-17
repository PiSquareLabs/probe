"""The Detector, per PROBE-detector-spec.md: one module, two tiers, one
output. Tier 1 (zscore_scan.py) shouts candidates; Tier 2 (change_point.py)
confirms every one of them, not just the loudest. No ranking, no graph, no
root cause -- that's Correlator work (spec §1, §5, §6).

Usage:
    python detector.py                    # one-shot scan, prints the result
    python detector.py --out result.json  # also write it to a file
"""
import argparse
import json
from datetime import datetime, timezone

from change_point import change_point
from zscore_scan import zscore_scan


def detector_scan(cp_lookback: int = 5, cp_bucket: int = 2, verbose: bool = False) -> dict | None:
    candidates = zscore_scan(verbose=verbose)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cp-lookback", type=int, default=5, help="Tier 2 lookback, minutes")
    ap.add_argument("--cp-bucket", type=int, default=2, help="Tier 2 bucket size, seconds")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    result = detector_scan(args.cp_lookback, args.cp_bucket, args.verbose)

    if result is None:
        print("No candidates. System looks stable.")
    else:
        print(f"{len(result['candidates'])} candidate(s), "
              f"loudest={result['loudest']}, earliest={result['earliest']}\n")
        for c in result["candidates"]:
            print(f"  tier={c['tier']}  {c['service']:18s} {c['signal']:16s} "
                  f"z={c['z']:6.2f}  type={c['type']}  "
                  f"pvalue={c['pvalue']}  @{c['timestamp']}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nWrote result to {args.out}")


if __name__ == "__main__":
    main()
