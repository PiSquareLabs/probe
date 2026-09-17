# PROBE — Detector Build Spec

One module, two tiers, one output. Built from what's already written: `probe-detector` (z-score scan) as the trigger, `causal-changepoint-detection`'s CHANGE_POINT query as the confirmer. The ranking layer of #2 moves out — it's Correlator work.

**The Detector answers three questions and no more:** *which services changed, when, how sure are we.* It never names a root cause, never names a dependency, never ranks.

---

## 1. Contract

**In:** nothing. It runs on a loop.

**Out:** one object per scan, or `None`:
```json
{
  "candidates": [
    { "service": "cart", "signal": "p95_latency", "type": "step_change",
      "timestamp": "10:42:02", "pvalue": 0.0003, "z": 8.1, "tier": 2 }
  ],
  "loudest":  "cart",
  "earliest": "cart",
  "scan_at":  "10:42:15"
}
```

**Never in the output:** `root_cause`, `dependency`, `rank`, anything from the graph, anything from the fault catalog.

---

## 2. Two tiers

### Tier 1 — the lookout (z-score, from `probe-detector`)
Glances at every service every 5–10s. Shouts the names of any `(service, signal)` that looks different from a minute ago. Fast because it's shallow. Doesn't know *how* or exactly *when*.

- 5 ES|QL queries: trace error rate, trace p95 latency, log error count, CPU, memory
- 30s buckets over 10 minutes (tighten to 5s / 5 min if the signal holds — test it)
- Recent 60s vs. baseline (everything else); `z = (recent − baseline) / stdev`; stdev = 0 and recent higher → force `z = threshold + 1`
- `z ≥ 3` → candidate
- **No ranking.** Over-inclusive on purpose.

### Tier 2 — the inspector (CHANGE_POINT, from `causal-changepoint-detection`)
Goes **only** where the lookout pointed. For each candidate, one query scoped to that service and that signal:

```esql
FROM traces-generic.otel-probe
| WHERE @timestamp > NOW() - 5 minutes
  AND service.name == "<candidate>"
  AND NOT span.name LIKE "flagd.evaluation*"
| STATS p95 = PERCENTILE(duration, 95) BY bucket = BUCKET(@timestamp, 2 seconds)
| CHANGE_POINT p95 ON bucket
| WHERE type IS NOT NULL AND pvalue < 0.01
```

- 5 min at 2s = 150 buckets. Over the 22 minimum, under the 1,000 cap.
- Returns `type` (spike / step_change / dip / distribution_change / trend_change), the precise `timestamp` of the break, and `pvalue`.
- No break → candidate stays in the list marked `tier: 1`. It is **not dropped**.

**Runs on every candidate, not just the first.** Lookout shouts four names → inspector runs four times. Every candidate gets its own verdict.

---

## 3. The combined loop

```python
def detector_scan():
    candidates = zscore_scan()                      # tier 1 — #3, unchanged
    if not candidates:
        return None

    confirmed = []
    for c in candidates:                            # ALL candidates, not the first
        cp = change_point(c.service, c.signal)      # tier 2 — #2's query, scoped
        confirmed.append({
            "service":   c.service,
            "signal":    c.signal,
            "type":      cp.type      if cp else None,
            "timestamp": cp.timestamp if cp else c.ts,
            "pvalue":    cp.pvalue    if cp else None,
            "z":         c.z,
            "tier":      2 if cp else 1,
        })

    return {
        "candidates": confirmed,
        "loudest":    max(candidates, key=lambda c: c.z).service,
        "earliest":   min((c for c in confirmed if c["tier"] == 2),
                          key=lambda c: c["timestamp"], default=confirmed[0])["service"],
        "scan_at":    now(),
    }
```

`change_point(service, signal)` is a **standalone function**, not buried in the loop. The Correlator calls it too (§6).

---

## 4. Worked example — 400ms latency on `valkey-cart`

Injected at **10:42:00**. valkey-cart is the Redis cache behind `cart`. It emits **no spans** — it is not a service in the telemetry.

Dependencies: `frontend → checkout → cart → valkey-cart`, and `frontend → cart` directly.

### 10:42:10 — Tier 1 shouts

| service | signal | baseline | recent | z | shout? |
|---|---|---|---|---|---|
| cart | p95_latency | 12ms | 415ms | **8.1** | yes |
| frontend | p95_latency | 180ms | 610ms | **5.4** | yes |
| checkout | p95_latency | 90ms | 505ms | **4.9** | yes |
| checkout | error_rate | 0.1% | 0.4% | **3.2** | yes |
| recommendation | p95_latency | 40ms | 44ms | 0.6 | no |
| *(others)* | | | | <3 | no |

Four candidates. `loudest = cart`.

Nobody walked from cart to frontend. Frontend was just also slow, and shouted on its own.

### 10:42:15 — Tier 2 inspects all four

| candidate | type | timestamp | pvalue | tier |
|---|---|---|---|---|
| cart / p95_latency | step_change | 10:42:02 | 0.0003 | 2 |
| checkout / p95_latency | step_change | 10:42:03 | 0.0009 | 2 |
| frontend / p95_latency | step_change | 10:42:04 | 0.0011 | 2 |
| checkout / error_rate | *(none)* | 10:42:10 | — | 1 |

Three confirmed. The error blip stays, marked tier 1. `earliest = cart` (10:42:02).

### Output, ~15s after injection

```json
{
  "candidates": [
    {"service":"cart",    "signal":"p95_latency","type":"step_change","timestamp":"10:42:02","pvalue":0.0003,"z":8.1,"tier":2},
    {"service":"checkout","signal":"p95_latency","type":"step_change","timestamp":"10:42:03","pvalue":0.0009,"z":4.9,"tier":2},
    {"service":"frontend","signal":"p95_latency","type":"step_change","timestamp":"10:42:04","pvalue":0.0011,"z":5.4,"tier":2},
    {"service":"checkout","signal":"error_rate", "type":null,         "timestamp":"10:42:10","pvalue":null,  "z":3.2,"tier":1}
  ],
  "loudest":  "cart",
  "earliest": "cart",
  "scan_at":  "10:42:15"
}
```

### What it did NOT say
It did not say `valkey-cart`. It can't — valkey-cart is in no query result. The Detector's job ends at *"cart is loudest and earliest, and it's a step change."*

### Where the answer comes from
The Correlator's deepest-span traversal finds cart's slow spans are all `HGET` with `span.kind = CLIENT`, `db.system = redis`, peer `valkey-cart`. Graph ranking confirms cart is depended on by checkout and frontend. LLM answers `(upstream_dependency_latency, valkey-cart)`. Correct.

### The baselines on the same fault
- **Threshold alert** — fires 10:42:30 when frontend p95 > 500ms. Names `frontend`. Two hops from the cause.
- **Loudest service** — reads `loudest = cart`. Names `cart`. One hop short.

That's symptom drift, on screen, from one fault.

---

## 5. Two problems at once

Same valkey-cart fault at 10:42:00. Separately, `payment` starts throwing 500s at **10:42:20**.

### 10:42:30 — Tier 1 shouts six
cart, frontend, checkout (latency), checkout (errors), payment (errors), frontend (errors).

### Tier 2 inspects six

| candidate | verdict |
|---|---|
| cart / latency | step_change at **10:42:02** |
| checkout / latency | step_change at 10:42:03 |
| frontend / latency | step_change at 10:42:04 |
| checkout / errors | *(no break)* |
| payment / errors | spike at **10:42:21** |
| frontend / errors | spike at 10:42:23 |

Two clusters of timestamps: one at 10:42:02, one at 10:42:21. **The Detector does not reason about that.** It reports all six with times and types.

The Correlator sees the twenty-second gap, the graph says cart's latency doesn't explain payment's errors, and the span traversal finds two different things — a Redis client span under cart, an HTTP 500 under payment. Two root causes, two answers.

### If instead the second problem was *caused* by the first
Same mechanics. If checkout's errors were cart timeouts bubbling up, CHANGE_POINT confirms them at ~10:42:05 — right after cart, not twenty seconds later. Earliness plus the graph (checkout depends on cart) puts cart upstream. One cause, several symptoms.

The difference between "two faults" and "one fault cascading" lives entirely in the timestamps and the graph. The Detector supplies the timestamps. It never interprets them.

---

## 6. The chain question: 1 → 10, and 5 shouts

Dependency chain `1 → 2 → … → 10`. Tier 1 shouts **5**.

**Does CHANGE_POINT automatically check 6–10?** No. It checks only what was shouted. The Detector doesn't know the graph exists.

**So how do 6–10 get checked?** They shout for themselves. If 5 is broken and 6–10 depend on it, their metrics move, their z crosses 3, they appear in the same scan. Nobody walks from 5 to 6.

**If 6–10 are silent?** They're probably fine. The Detector doesn't invent anomalies in services that didn't move.

### The case that actually matters — the quiet upstream cause
Service **3** is the cause. Its latency went 5ms → 9ms: a 2× change, but z = 2.1, under the bar. Downstream 5, 7, 9 are screaming.

Tier 1 shouts 5, 7, 9. Tier 2 confirms them. **Nobody checks 3.**

### Where the fix lives — not in the Detector
`change_point(service, signal)` is exposed as a function. The **Correlator owns the graph**, so when it receives the list it can ask for one more hop toward dependencies:

```
Detector:   shouts 5, 7, 9 → CHANGE_POINT on 5, 7, 9 → hands over
Correlator: "what does 5 call?" → 3, 4
            → change_point(3), change_point(4)
            → 3 shows step_change at 10:42:01, earlier than 5
            → 3 is upstream AND earliest → strong candidate
```

Now 3 has a p-value and a timestamp even though it never crossed the z-score bar. Two or three extra queries, triggered by the component that knows why it's asking. Graph logic stays in one place.

---

## 7. Changes to the existing code

| # | Repo | Change |
|---|---|---|
| 1 | `probe-detector` | Keep as `zscore_scan()`. Optionally tighten to 5s buckets / 5 min. |
| 2 | `causal-changepoint-detection` | Extract the CHANGE_POINT query into `change_point(service, signal)`. Add `service.name ==` filter. Bucket 20–30s → **2s**. Lookback 15–30 min → **5 min**. |
| 3 | `causal-changepoint-detection` | **Delete the ranking layer from the detector.** Move earliness + depended-on-by scoring and `service_graph.json` to the Correlator. |
| 4 | both | Move the flagd `EventStream` exclusion into one shared fragment (or an ingest pipeline `drop` on `span.name`). Four copies of the same filter is four places to get it wrong. |
| 5 | `elastic-ml-anomaly-detection` | `bucket_span` 5m → **30s–1m**, backfill, run in parallel. Log its score as a fourth signal for the ablation. **Not** on the critical path. |
| 6 | `ml-flag-detection` | Never on the graded path. Keep as a baseline row: *supervised classifier trained on the answer key: 55%.* Check its feature extraction does not read `feature_flag.*`. |

---

## 8. Day 0 experiment

Same injected fault, three bucket spans for `change_point()`: **1s / 2s / 5s**. Pick the smallest with a clean `pvalue < 0.01`. That number decides the demo timing. Twenty minutes.

---

## 9. Timing budget

| Moment | Elapsed |
|---|---|
| Fault injected | 0s |
| Tier 1 shouts | ~10s |
| Tier 2 confirms, object handed to Correlator | ~15s |

Everything after that is the Correlator and the LLM.

---

## 10. Deliberately not here

- **No LLM.** Detection is statistics.
- **No graph.** The Correlator owns it and asks for extra hops.
- **No ranking.** Every candidate goes out with its own verdict.
- **No ML classifier.** Baseline only.
- **No Elastic ML AD on the critical path.** Parallel signal, ablation row.

---

## 11. One line for the diagram

> *Tier 1: z-score scan every 10s → candidates. Tier 2: CHANGE_POINT on every candidate → type, timestamp, p-value. No ranking, no cause.*

## 12. One sentence for the pitch

The z-score says *who*, CHANGE_POINT says *when and how sure* for every one of them, and the Correlator says *why* — including whether it's one problem or two.
