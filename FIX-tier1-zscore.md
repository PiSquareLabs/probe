# Fix: Tier 1 z-score detector (`probe-detector` / `zscore_scan()`)

Scope: the Tier 1 trigger only. Do not touch Tier 2 (CHANGE_POINT), the Correlator, or the validator except where §6 says.

## Why

Validation run: 2/11 faults detected. Every miss was Tier 1 never shouting. Same code previously scored 5/11. Four mechanical causes, each visible in the arithmetic:

1. **Recent window dilutes the fault.** Recent = 2 × 30s buckets (60s). Faults last 25–40s. Recent buckets hold a mix of faulted and healthy seconds; `recent_value` lands at ~60–70% of the true faulted value.
2. **Baseline stdev is inflated by host contention.** Contention spikes in the 18 baseline buckets push stdev from ~20 to ~80; every z shrinks 4×. Sensitivity drops exactly when the environment is noisy.
3. **60 independent checks per scan, every ~10s.** At z ≥ 3, ~0.16 expected false shouts per scan on Gaussian data; latency is heavy-tailed, so worse.
4. **One threshold for five signals with different noise profiles.** Error rate near 0.001 with stdev ~0.001 produces z = 15 from two extra errors.

Worked case for cause 1 + 2, 35s fault raising latency to 610ms over a 180ms baseline:

```
bucket A: 20s fault + 10s healthy → avg 467
bucket B: 15s fault + 15s healthy → avg 395
recent_value = 431  (not 610)
calm host,     stdev 20: z = 12.5  → shouts
contended host, stdev 80: z = 3.1  → barely
fault mostly in one bucket, stdev 80: recent ≈ 350, z = 2.1 → MISS
```

---

## Changes

### C1 — Recent window: 3 × 10s buckets, drop the in-progress bucket

**Current:** query 10 min at 30s buckets; recent = last 2 buckets (60s); baseline = the rest (~18 buckets).

**New:**
- Query the last 10 minutes at **10s buckets** (~60 buckets).
- **Exclude the most recent bucket** if it is still filling: `bucket_end > now()` or, equivalently, the bucket whose start is within 10s of `now()`. A 5-second-old bucket averages a handful of spans and is noise.
- **Recent** = the **3 most recent complete buckets** (30s).
- **Baseline** = everything before those 3 (~56 buckets).

Keep baseline and recent decoupled as they are now — do not shorten the baseline to shorten the recent window.

Rationale: recent window ≈ fault duration → no dilution. 10s buckets still hold enough spans at demo traffic; if a bucket has fewer than `MIN_SPANS_PER_BUCKET` (default 20) spans, treat it as missing rather than averaging it.

### C2 — Robust z: median + MAD

**Current:**
```
z = (recent_mean − baseline_mean) / baseline_stdev
```

**New:**
```
baseline_median = median(baseline_buckets)
mad             = median(|b − baseline_median| for b in baseline_buckets)
recent_median   = median(recent_buckets)          # median of the 3 recent buckets
z               = 0.6745 × (recent_median − baseline_median) / mad
```

Special case, unchanged in spirit: if `mad == 0` and `recent_median > baseline_median`, force `z = THRESHOLD + 1`. If `mad == 0` and recent is not higher, `z = 0`.

Rationale: median and MAD are not moved by the contention spikes that inflate mean and stdev. The 0.6745 constant makes MAD comparable to stdev for Gaussian data, so `THRESHOLD = 3.0` keeps its meaning.

### C3 — Persistence: two consecutive scans

**New:** maintain `streak[(service, signal)]`, an integer.

```
if z >= THRESHOLD: streak[key] += 1  else: streak[key] = 0
shout only if streak[key] >= PERSISTENCE   # default 2
```

Reset the streak on every scan where the pair is under the bar. State lives in memory for the process lifetime; no persistence to disk needed.

Rationale: a false spike from 60 checks/scan rarely survives two consecutive scans; a real fault does. Costs one scan interval (~10s).

Make `PERSISTENCE` configurable. The validator's tier-1-only mode may set it to 1 for comparison runs.

### C4 — Minimum count floor on error signals

Applies to `error_rate` (trace failures) and `log_errors` only. Not latency, CPU, memory.

**New:** alongside the rate, compute the raw count of failed spans / error logs in the recent window. A candidate on these signals is shouted only if:

```
z >= THRESHOLD  AND  recent_error_count >= MIN_ERROR_COUNT   # default 5
```

Rationale: a proportion with a near-zero baseline produces enormous z from tiny absolute changes. The floor stops that without touching the latency path.

### C5 — Latency per bucket: percentile, not mean

**Current:** one average value per bucket for latency.

**New:** use `PERCENTILE(duration, 95)` per bucket for the latency signal (and optionally `PERCENTILE(duration, 50)` as a second latency signal). Mean latency is dominated by a few slow outliers inside a bucket; p95 is what Tier 2 already uses, so the two tiers see the same shape.

Keep the existing flagd `EventStream` span exclusion. Move it to one shared fragment if it isn't already.

---

## Config surface (add to the existing config)

```
BUCKET_SECONDS          = 10
LOOKBACK_MINUTES        = 10
RECENT_BUCKETS          = 3
EXCLUDE_PARTIAL_BUCKET  = true
MIN_SPANS_PER_BUCKET    = 20
THRESHOLD               = 3.0
PERSISTENCE             = 2
MIN_ERROR_COUNT         = 5
ROBUST_Z                = true      # false → old mean/stdev path, for A/B
```

Keep the old mean/stdev path behind `ROBUST_Z = false` so the validator can run both.

---

## Output shape — unchanged, plus two fields

Each candidate keeps `service, signal, z, ts`. Add:

- `streak` — the consecutive-scan count at shout time
- `recent_count` — raw error count for error signals, span count for latency

Tier 2 ignores both. The validator uses them.

---

## Tests to add

1. **Dilution test.** Synthetic series: 56 baseline buckets at 180 ± 20, then a 35s fault at 610 starting mid-bucket. Old config must produce `recent_value < 450`; new config must produce `recent_median ≥ 550`.
2. **Contended-baseline test.** Same series, but inject 4 contention spikes (400, 500, 450, 380) into the baseline. Old z must fall below 3.0; robust z must stay above 3.0.
3. **Persistence test.** One bucket at z = 5, then back to baseline. Must not shout. Two consecutive scans at z ≥ 3 must shout.
4. **Error floor test.** Baseline error rate 0.001, recent 0.004 with `recent_error_count = 2`. Must not shout. Same with count 8. Must shout.
5. **Partial bucket test.** Most recent bucket has 3 spans. Must be excluded from recent.

---

## Validation rerun (§6 — the only validator change)

After the fixes, rerun the battery with:

- **5 cycles per flag**, not 1
- **3 null windows** (no fault) in the same battery, same host, same time
- **Fault duration ≥ 60s** if you can't guarantee it lands inside the 30s recent window; otherwise keep 25–40s and let C1 handle it
- Score **two ways** and report both: any-tier detection (Tier 1 shouted) and tier-2-confirmed
- Break misses down **by signal type** (latency vs. error vs. resource)
- Report **false shouts on null windows** as a separate number

A result is: `detected_any / 11`, `confirmed_t2 / 11`, `false_shouts / 3 nulls`, per-signal miss table. Not one number.

---

## Order

C1 → C2 → C3 → C4 → C5 → tests → rerun. C1 and C2 are expected to move the number most; do them first and rerun once before the rest if time is short.

## Not in scope

- Tier 2 bucket size (that's the Day 0 experiment, separate)
- Anything in the Correlator
- The Elastic ML anomaly job
- The ml-flag-detection classifier
