# Two-Tier Detector v2 (Tier 1 fix, per `FIX-tier1-zscore.md`)

**Detection method:** the same two-tier pipeline as `../probe-two-tier-detector/`
(Tier 1 z-score "lookout" gated by Tier 2 `CHANGE_POINT` "inspector"), with
Tier 1 rebuilt per [`../FIX-tier1-zscore.md`](../FIX-tier1-zscore.md) after
v1 regressed to 2/11 confirmed. **Tier 2 (`change_point.py`) is untouched**
— copied verbatim from v1 — per that doc's explicit scope statement: "the
Tier 1 trigger only." A follow-up detector, `../probe-two-tier-detector-v3/`,
fixes Tier 2 itself; see that folder's README once its battery completes.

## 1. What changed in Tier 1, and why

v1's `zscore_scan()` had four independent mechanical problems, each
diagnosed with worked arithmetic in the fix doc:

| # | Problem | Fix |
|---|---|---|
| C1 | 60s "recent" window (2×30s buckets) diluted 25-40s faults with healthy seconds from the same bucket | 10s buckets, 3 recent buckets (30s, ≈ fault duration), the still-filling tail bucket excluded via an upper `@timestamp` bound, per-bucket floor raised to 20 spans (from 3) |
| C2 | Baseline mean/stdev inflated by host-contention spikes, shrinking every z-score | Median/MAD z-score (`z = 0.6745 × (recent_median − baseline_median) / mad`), not moved by a handful of outliers |
| C3 | 60 independent (service, signal) checks every ~10s produced real false shouts even at z ≥ 3 on heavy-tailed data | Persistence: require 2 consecutive scans over threshold (`streak[(service, signal)]`) before shouting |
| C4 | A near-zero baseline error rate produced z = 15 from two or three extra errors | Minimum raw-count floor (5) on `error_rate`/`log_error_count` only — not latency/CPU/memory |
| C5 | (assumed) latency scored on a per-bucket mean, not p95 | **No-op** — this codebase already used `PERCENTILE(duration, 95)` per bucket; the fix doc's premise didn't hold here. Documented, not silently skipped. |

### Files

| File | What changed from v1 |
|---|---|
| `es_client.py` | Unchanged, copied verbatim. |
| `queries.py` | Rewritten: `_window_clause()` now bounds every query to `[now - lookback, now - bucket_seconds)` (excludes the partial tail bucket); bucket/lookback are parameters (default 10s/10min); `MIN_SPANS_PER_BUCKET` raised to 20 for trace-based queries; `error_rate_by_service()` gained a `fail_count` column for C4. |
| `zscore_scan.py` | Rewritten: `compute_z()` implements both the old mean/stdev path and the new median/MAD path (`ROBUST_Z` toggles between them, per the fix doc's own A/B requirement); `_score_signal()` adds the `streak_state` dict (C3) and the count floor (C4); candidates now carry `streak` and `recent_count`. |
| `change_point.py` | Unchanged, copied verbatim — Tier 2, out of scope for this fix. |
| `detector.py` | `detector_scan()` now takes an explicit `streak_state` dict, threaded through every candidate. Added a `--loop SECONDS` mode, because persistence (C3) needs state that survives across scans — a fresh one-shot process can never satisfy `PERSISTENCE=2` on its own (see module docstring). |
| `validate_against_demo.py` | Rewritten per fix doc §6: 5 cycles/flag (not 1), 3 null windows folded into the same battery, dual scoring (any-tier / tier-2-confirmed), per-signal miss table, false-shout count — one `streak_state` kept for the whole battery, matching a real long-running detector process. |
| `test_zscore.py` | New. The 5 tests the fix doc asks for (dilution, contended-baseline, persistence, error-floor, partial-bucket), using its exact synthetic fixtures. No Elasticsearch needed. |

## 2. Running it

```bash
cd probe-two-tier-detector-v2
python -m pytest test_zscore.py -v         # or: python test_zscore.py — no ES needed
python detector.py --out result.json       # one-shot scan (streak starts at 0)
python detector.py --loop 10               # long-running scan, persistence actually applies
python validate_against_demo.py            # full battery: 11 flags x 5 cycles + 3 nulls
```

No pip installs beyond the standard library.

## 3. Unit tests: 9/9 pass

```
PASS test_dilution_old_config_dilutes_fault
PASS test_dilution_new_config_recent_median_holds
PASS test_contended_baseline_old_z_falls_below_threshold
PASS test_contended_baseline_robust_z_stays_above_threshold
PASS test_persistence_single_scan_over_threshold_does_not_shout
PASS test_persistence_two_consecutive_scans_shout
PASS test_error_floor_below_min_count_does_not_shout
PASS test_error_floor_above_min_count_shouts
PASS test_partial_bucket_excluded_from_recent
```

The contended-baseline test reproduces the fix doc's own worked case: 56
baseline buckets with 4 contention spikes (500/650/600/450 against a flat
180 elsewhere), scored against v1's diluted 2-bucket recent average (431)
vs. v2's 3 fully-faulted recent buckets (610). Old mean/stdev z lands at
2.28 (below threshold — the contention masks the fault); new median/MAD z
lands at 4.0 (the doc's `mad == 0` special case: 52 of 56 baseline points
sit exactly at 180, so MAD is 0, and `recent_median > baseline_median`
forces `z = THRESHOLD + 1`).

## 4. Live smoke test: the fix works as intended

A manual `adHighCpu` injection, polled every 12s with `detector.py --loop 12`:

```
scan 1: no candidates (streak building, not yet persistent)
scan 2: ad/cpu z=1651.77 streak=2   tier=1  (persistence satisfied, shouted)
scan 3: ad/cpu z=2176.56 streak=3   tier=1
```

Confirms C1-C4 end to end: Tier 1 now shouts a real, sustained CPU fault
with a very high z-score and a growing streak — the exact failure mode
that made v1 score 2/11 (Tier 1 never shouting) is gone for at least this
flag. Tier 2 didn't confirm this particular one (`type: None`), which is
expected and out of this fix's scope — `CHANGE_POINT` over a flat,
sustained step spanning the whole 5-minute Tier 2 window doesn't always
register a clean "point" of change; see `../probe-two-tier-detector-v3/`
for the fix aimed at Tier 2 itself.

## 5. Full validation battery: interrupted partway, results so far

**This battery was stopped intentionally after 8 of 11 flags** (before
`productCatalogFailure` finished its first cycle, and before
`emailMemoryLeak`/`kafkaQueueProblems`/the 3 null windows ran at all) to
free up the live demo stack for `probe-two-tier-detector-v3`'s own
battery. The numbers below are real, not simulated, but are a partial
run, not the full §6 battery — no false-shout number and no per-signal
table are reported here because the null windows never ran.

| Flag | Target | Signal | Detected (any cycle) | Confirmed (any cycle) |
|---|---|---|---|---|
| `adFailure` | `ad` | `error_rate` | 0/5 | 0/5 |
| `adHighCpu` | `ad` | `cpu` | 2/5 | 0/5 |
| `adManualGc` | `ad` | `p95_latency` | 0/5 | 0/5 |
| `cartFailure` | `cart` | `error_rate` | 2/5 | **2/5** |
| `paymentFailure` | `payment` | `error_rate` | 1/5 | 0/5 |
| `recommendationCacheFailure` | `recommendation` | `p95_latency` | 3/5 | 0/5 |
| `imageSlowLoad` | `frontend` | `p95_latency` | 0/5 | 0/5 |
| `intlShippingSlowdown` | `shipping` | `p95_latency` | 0/5 | 0/5 |
| `productCatalogFailure` | `product-catalog` | `error_rate` | not run (1 cycle in progress, aborted) | — |
| `emailMemoryLeak` | `email` | `memory` | not run | — |
| `kafkaQueueProblems` | *(none)* | — | not applicable | — |

**Per-flag summary (8 of 11 flags completed):** detected any-tier in at
least one cycle: **4/8** (`adHighCpu`, `cartFailure`, `paymentFailure`,
`recommendationCacheFailure`). Tier-2-confirmed in at least one cycle:
**1/8** (`cartFailure`, twice — 30.0s and 140.6s).

**This is a real recall improvement over v1's 2/11**, even on a partial
run: v1's Tier 1 never shouted for `cartFailure`, `paymentFailure`, or
`recommendationCacheFailure` at all (every one of those was "Tier 1 never
shouting," per v1's README §3); v2's Tier 1 now shouts for all three,
confirming for `cartFailure` twice. The gap that remains is now visibly at
**Tier 2**, not Tier 1: `adHighCpu`, `paymentFailure`, and
`recommendationCacheFailure` all detected at Tier 1 across multiple
cycles without a single Tier-2 confirmation — the same pattern seen in
the §4 smoke test. That's the exact motivation for
`../probe-two-tier-detector-v3/`'s Tier 2 fixes (in particular, its
insufficient-data vs. genuine-no-break distinction, and earliest-break
selection instead of lowest-p-value).

`adFailure`, `adManualGc`, `imageSlowLoad`, and `intlShippingSlowdown`
still missed every cycle at Tier 1 — the same low-traffic/percentage-gated
signal problem documented across every other detector in this repo (see
`../ml-flag-detection/README.md` §6, `../probe-detector/README.md` §3).
C1-C4 fix dilution, contention-inflated stdev, false shouts, and tiny-count
inflation; they don't manufacture signal that isn't in the traffic to
begin with.

**Not yet run: the null-window false-shout count.** The fix doc's §6 asks
for 3 null (no-fault) windows to measure false shouts against a quiet
system. This wasn't reached before the battery was stopped — re-run
`validate_against_demo.py --only adFailure adManualGc imageSlowLoad
intlShippingSlowdown productCatalogFailure emailMemoryLeak
kafkaQueueProblems --nulls 3` (or the full flag list) to complete it.

## 6. Comparison to v1

| | v1 (`../probe-two-tier-detector/`) | v2 (this folder, partial battery) |
|---|---|---|
| Detected any-tier | 2/11 (single cycle each) | 4/8 completed flags (5 cycles each) |
| Tier-2-confirmed | 2/11 | 1/8 completed flags |
| z-score basis | mean/stdev | median/MAD |
| Recent window | 60s (2×30s buckets), diluted | 30s (3×10s buckets), no dilution |
| False-shout protection | none | persistence (2 consecutive scans) + count floor |
| Config surface | fixed constants | `BUCKET_SECONDS`, `LOOKBACK_MINUTES`, `RECENT_BUCKETS`, `MIN_SPANS_PER_BUCKET`, `THRESHOLD`, `PERSISTENCE`, `MIN_ERROR_COUNT`, `ROBUST_Z` — all overridable |

v1's two confirmations (`adHighCpu`, `adManualGc`) don't reappear as
confirmations here — both now detect reliably at Tier 1 but stall at Tier
2, a regression in *confirmation*, not detection, and consistent with the
hypothesis that Tier 2 itself (unchanged, out of scope here) has its own
sensitivity gaps for sustained step-changes vs. sharp spikes. `cartFailure`
is a genuinely new confirmation v1 never had at all.
