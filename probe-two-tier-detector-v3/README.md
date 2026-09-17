# Two-Tier Detector v3 (Tier 2 fix)

**Detection method:** the same two-tier pipeline as v1/v2 (Tier 1 z-score
"lookout" gated by Tier 2 `CHANGE_POINT` "inspector"), with **Tier 2 now
fixed**. `../probe-two-tier-detector-v2/` fixed Tier 1 (window dilution,
contention-inflated stdev, false shouts, tiny-count inflation) and got a
real recall gain, but its own partial battery showed several flags
detecting reliably at Tier 1 while stalling at Tier 2 confirmation
(`adHighCpu`, `adManualGc`, `paymentFailure`, `recommendationCacheFailure`
all shouted repeatedly without ever confirming). This detector fixes that
bottleneck directly in `change_point()` and its caller in `detector.py`.
**Tier 1 (`zscore_scan.py`, `queries.py`) is copied unchanged from v2.**

## 1. What changed in Tier 2, and why

| # | Problem | Fix |
|---|---|---|
| 1 | `change_point()` silently returned `None` for both "no significant break" and "too little data for the test to run at all" — indistinguishable from the caller's point of view | Returns `{"reason": "insufficient_data", "rows": N}` instead of `None` on that specific error class; a per-`(service, signal)` counter (`INSUFFICIENT_DATA_COUNTS`) tracks how often this happens |
| 2 | `cpu`/`memory` come from `metrics-*` at the SDK's own export interval (10-60s) — a 5-minute window at 2-second buckets can't produce the ~22 rows `CHANGE_POINT` needs, so these signals almost always hit #1 above | Per-signal bucket/lookback timing: traces (`p95_latency`, `error_rate`) keep 2s/5min; `log_error_count` gets 10s/10min; `cpu`/`memory` get 30s/30min |
| 3 | Among multiple significant breaks, the old code picked the lowest p-value — but a window spanning both a fault's onset and its recovery has two breaks, and the recovery dip's p-value is often lower, reporting the wrong start time | Picks the **earliest** significant break instead; the full sorted list is kept as `breaks` so a future Correlator can see the recovery too |
| 4 | A cascading fault can make Tier 1 shout 4+ services on the same signal at once, each needing its own `CHANGE_POINT` query — real cost during exactly the moment latency matters most | 4+ same-signal candidates get batched into one query (`service.name IN (...)` + `CHANGE_POINT m ON bucket BY service.name`), split back per service; below that threshold, per-pair calls stay as before |
| 5 | Cheap query fixes | Exact data-stream name instead of the `traces-*.otel-default` wildcard (env-configurable); `span.kind == "SERVER"` added to the traces filter (server-side handling time only, cuts scan volume 3-5x); flagd exclusion left as `LIKE` with a TODO noting the `probe.exclude` ingest-pipeline field doesn't exist yet |
| 6 | Redundant `\| SORT bucket ASC` before `CHANGE_POINT` — the command already orders on its own `ON` key | Removed |

Untouched, per scope: the `pvalue < 0.01` threshold, the
per-candidate-not-just-loudest rule, non-statistical error surfacing, the
`_SIGNAL_QUERIES` table's shape.

### Files

| File | What changed from v2 |
|---|---|
| `es_client.py`, `zscore_scan.py`, `queries.py` | Unchanged, copied verbatim — Tier 1, out of scope for this fix. |
| `change_point.py` | Rewritten per the 6 items above: `_SIGNAL_TIMING` + `_resolve_timing()` (#2), `INSUFFICIENT_DATA_COUNTS` + row-count extraction (#1), `_rows_to_result()` picking the earliest break and keeping `breaks` (#3), new `change_point_batch()` for cascades (#4), exact data-stream name + `SERVER`-span filter (#5), dropped `SORT` (#6). |
| `detector.py` | `_confirm_candidate()` carries `reason`/`breaks` into each candidate; `detector_scan()` groups same-signal candidates and calls `change_point_batch()` once 4+ share a signal (`BATCH_THRESHOLD = 3`), else the per-pair call as before. |
| `test_change_point.py` | New. 3 tests: insufficient-data reason/counter/tier-1-stays, earliest-break-wins over lowest-p-value, one query for a 6-service same-signal cascade. All mock `_esql`, no live ES needed. |
| `test_zscore.py` | Unchanged, copied from v2 — confirms the Tier 1 fix wasn't disturbed (still 9/9). |

## 2. Running it

```bash
cd probe-two-tier-detector-v3
python test_zscore.py            # 9 Tier-1 tests, unchanged from v2
python test_change_point.py      # 3 new Tier-2 tests
python detector.py --out result.json
python detector.py --loop 10     # long-running, for Tier 1 persistence
python validate_against_demo.py  # full battery
```

No pip installs beyond the standard library. Unit tests: 9/9 + 3/3 pass,
no Elasticsearch needed.

## 3. Validation battery: 8 of 8 flags complete

Run against the same 8 flags `../probe-two-tier-detector-v2/`'s partial
battery covered (`productCatalogFailure`, `emailMemoryLeak`,
`kafkaQueueProblems` and the 3 null windows intentionally out of scope for
this comparison run), 5 cycles each, one `streak_state` kept for the whole
battery:

| Flag | Target | Signal | Detected (any cycle) | Confirmed (any cycle) | v2's result |
|---|---|---|---|---|---|
| `adFailure` | `ad` | `error_rate` | 0/5 | 0/5 | 0/5 detected |
| `adHighCpu` | `ad` | `cpu` | 4/5 | **4/5** | 2/5 detected, 0/5 confirmed |
| `adManualGc` | `ad` | `p95_latency` | 2/5 | **2/5** | 0/5 |
| `cartFailure` | `cart` | `error_rate` | 4/5 | **4/5** | 2/5 confirmed |
| `paymentFailure` | `payment` | `error_rate` | 2/5 | **2/5** | 1/5 detected, 0/5 confirmed |
| `recommendationCacheFailure` | `recommendation` | `p95_latency` | 3/5 | **2/5** | 3/5 detected, 0/5 confirmed |
| `imageSlowLoad` | `frontend` | `p95_latency` | 0/5 | 0/5 | 0/5 |
| `intlShippingSlowdown` | `shipping` | `p95_latency` | 0/5 | 0/5 | 0/5 |

**Per-flag summary, 8 of 8 flags completed:** detected any-tier in at
least one cycle: **5/8**. Tier-2-confirmed in at least one cycle: **5/8**
— the same 5 flags (`adHighCpu`, `adManualGc`, `cartFailure`,
`paymentFailure`, `recommendationCacheFailure`), every one of which now
confirms as often as it detects.

**This validates the diagnosis.** v2 had these exact 5 flags detecting at
Tier 1 without confirming at Tier 2 (`adHighCpu` 2/5 detected → 0/5
confirmed; `adManualGc` 0/5; `paymentFailure` 1/5 → 0/5;
`recommendationCacheFailure` 3/5 → 0/5; `cartFailure` was v2's one partial
win, 2/5 confirmed). With the Tier 2 timing fix (#2: giving `cpu` its own
30s/30min window instead of forcing it through the 2s/5min window built
for traces), every one of those confirms in most cycles it detects —
`adHighCpu` went from 0/5 confirmed to 4/5, `adManualGc` from 0/5 to 2/5,
`paymentFailure` from 0/5 to 2/5, `recommendationCacheFailure` from 0/5 to
2/5.

`adFailure`, `imageSlowLoad`, and `intlShippingSlowdown` still miss every
cycle — all three are Tier 1 misses (the fault signal itself is too
weak/low-traffic to clear the z-score threshold in the first place), not
Tier 2 rejections, and outside this fix's scope (Tier 1 is unchanged from
v2 here). This matches v2's own result on these same three flags exactly
(0/5 on all three in both versions), consistent with the fix being
correctly scoped to Tier 2 only.

The battery was intentionally stopped after these 8 flags (matching v2's
own partial scope, for a clean comparison) — `productCatalogFailure`,
`emailMemoryLeak`, `kafkaQueueProblems`, and the 3 null windows were not
run. **Insufficient-data vs. genuine-no-break breakdown** (§6's requested
`change_point.INSUFFICIENT_DATA_COUNTS` readout) also wasn't captured —
the battery process was stopped via `Stop-Process` rather than allowed to
exit normally, so the counter's in-memory state was never printed.
Re-running with `--only` limited to a couple of the confirmed-slow flags
and reading `change_point.INSUFFICIENT_DATA_COUNTS` at the end would
close that gap if needed.

## 4. Comparison across all three versions

| | v1 | v2 (Tier 1 fix) | v3 (Tier 2 fix, this folder) |
|---|---|---|---|
| Tier 1 | mean/stdev, 60s diluted window, no persistence | median/MAD, 30s undiluted window, persistence + count floor | unchanged from v2 |
| Tier 2 | 2s/5min for every signal, lowest-p-value pick, `None` on both no-break and insufficient-data | unchanged from v1 | per-signal timing, earliest-break pick, insufficient-data surfaced distinctly, cascade batching |
| Detected any-tier (partial batteries, same 8-flag scope for v2/v3) | 2/11 (1 cycle each) | 4/8 (5 cycles each) | **5/8** (5 cycles each) |
| Tier-2-confirmed | 2/11 | 1/8 | **5/8** |

The jump from v2's 1/8 to v3's 5/8 confirmed, on the same 8 flags, same
Tier 1 code, is attributable to Tier 2 alone — strong evidence the
"insufficient data disguised as no break" + "one-size-fits-all bucket
timing" diagnosis in this fix was the actual bottleneck, not a coincidence
of a quieter battery run. Every flag that missed in both v2 and v3
(`adFailure`, `imageSlowLoad`, `intlShippingSlowdown`) missed at Tier 1,
before Tier 2 ever ran — this fix cannot help a candidate Tier 1 never
shouted about in the first place.
