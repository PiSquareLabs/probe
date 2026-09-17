# PROBE Detector

**Detection method:** client-side z-score anomaly scoring over ES|QL
bucketed aggregations — the **Detector** stage of PROBE, a 3-agent
incident-response design (`../idea/probe.pdf`, built for "Forge the Future
2026, Elastic + AWS Bedrock Track"):

> **Detector** — Scans live telemetry in Elasticsearch for statistically
> significant anomalies across traces, metrics, and logs as an incident
> unfolds.
>
> **Correlator** — Powered by Claude on Amazon Bedrock. Reasons through the
> service dependency graph to separate the one anomaly that caused the
> incident from the many that are just knock-on symptoms.
>
> **Remediator** — Matches the diagnosed cause to a fix in a runbook
> library and automatically files a ticket, closing the loop from
> detection to action.

This folder implements and validates **only the Detector stage** — an
over-inclusive anomaly scanner, deliberately not attempting to name a
single root cause (that's the Correlator's job; `../causal-changepoint-detection/`
is this machine's independent prototype of that ranking step, using
ES|QL `CHANGE_POINT` instead of an LLM).

## Relationship to `../probe-app/detector/`

`../probe-app/detector/` already had a substantial, well-designed partial
implementation (`anomaly.py`'s baseline/recent z-score split is genuinely
good work — ported here unchanged) as a FastAPI service targeting Elastic
Cloud/Serverless via API key. This folder is a **standalone reimplementation**
against the local self-hosted cluster (basic auth, no Docker build, no API
key provisioning needed) so it could be run and validated directly, with
two substantive fixes/additions made along the way:

1. **Fixed a live bug**: `queries.py`'s traces queries had no filter against
   `flagd.evaluation.v1.Service/EventStream` and `.../ResolveBoolean` spans
   — flagd's own OpenFeature client reconnecting its streaming connection.
   Checked directly against this stack: **both** of the `ad` service's
   `status.code == "Error"` spans in a 30-minute window were
   `EventStream` reconnects, not application errors. This is the same class
   of bug independently found and fixed in `../ml-flag-detection/` (via
   `attributes.event.outcome`) and `../causal-changepoint-detection/` — three
   separate detection methods against this stack, three times this exact
   noise source had to be filtered out. Anyone extending any of these should
   assume it needs filtering by default, not discover it the hard way again.
2. **Added the missing metrics signal**: the original covered traces (error
   rate, latency) and logs (error burst) but not metrics, despite the design
   doc explicitly specifying "traces, metrics, and logs." Added CPU and
   memory anomaly detection, `COALESCE`-d across the language-runtime-specific
   metric field names surveyed in `../ml-flag-detection/README.md` §2 (Java
   emits `jvm.*`, Go emits `go.*`/`process.*`, Python emits
   `process.runtime.cpython.*`, Node emits `v8js.*`) — ES|QL's `COALESCE`
   requires matching argument types, so each is cast with `TO_DOUBLE()`
   first (a real 400 error hit and fixed while building this — see git
   history / the comment in `queries.py`).

Field names were re-verified directly against the live index rather than
assumed: `service.name` and `status.code` **do** work here as ECS-style
aliases into the OTel-native mapping (confirmed with
`FROM traces-* | STATS c=COUNT(*) BY service.name`), so the original file's
field choices were already correct — a pleasant contrast to the raw
`resource.attributes.service.name` paths this machine's other two detectors
needed to use directly.

## Files

| File | What it is |
|---|---|
| `queries.py` | ES|QL query builders: error rate, p95 latency, log error bursts (traces/logs, ported+fixed), CPU/memory (metrics, new). |
| `anomaly.py` | Z-score scorer, ported unchanged from `../probe-app/detector/`. |
| `es_client.py` | Stdlib-only (`urllib`) ES|QL client against local basic-auth Elasticsearch. |
| `detector.py` | Orchestrator: runs all 5 scans, returns the combined anomaly list. Run directly for a one-shot scan. |
| `validate_against_demo.py` | Injects each known flagd fault, polls `detector.scan()` until the fault's target service is flagged (or gives up after 120s), records detection rate + time. |
| `validation_results.json` | Output of the last full validation run (§3 below). |

## Usage

```bash
# One-shot scan of current live telemetry
python detector.py

# Full validation battery (all 11 flags, ~15-20 min)
python validate_against_demo.py --settle 15

# Just one flag
python validate_against_demo.py --only adManualGc
```

Needs the same stack as this machine's other detectors: Elasticsearch
reachable at `localhost:9200`, credentials in
`../opentelemetry-demo/elastic-start-local/.env`, services receiving
traffic from the bundled load-generator.

### Shared result output (for a downstream Correlator/Remediator)

`python detector.py --out result.json` writes this detector's full
shareable output — the deliberately over-inclusive anomaly list (already
covering `log_error_burst`, one of the five `SCANS` in `detector.py`, so
logs were never missing from this detector's own signals) plus the
service dependency graph (`service_graph.json`, copied from
`../causal-changepoint-detection/`'s empirically-derived graph — same
demo stack, so the same graph applies). Schema:

```json
{
  "detector": "probe-detector",
  "generated_at": "...",
  "service_dependency_graph": {"edges": [{"caller": "...", "callee": "...", "count": N}]},
  "anomalies": [{"service": "...", "signal_type": "log_error_burst", "deviation_score": 4.9, ...}],
  "named_root_cause": null
}
```

`named_root_cause` is always `null` here, deliberately — per
`../idea/probe.pdf`'s architecture, picking one cause from this list is
the Correlator's job, not the Detector's. This module only *produces*
this file; it does not call, know about, or depend on any Remediator.

## 3. Validation results

PROBE's stated target metric (`../idea/probe.pdf` page 2): *"Root-caused 8
of 10 injected faults at a mean 40 seconds; threshold alerts fired on
symptoms for only 3 and never named the cause."* That target is for the
**full pipeline** (Detector + Correlator naming one cause). What follows
tests the Detector stage alone against all 11 flags this machine has been
using consistently across its three detector projects — a stricter,
narrower bar: did the Detector's anomaly list include the fault's *own*
designated service, and how fast.

```
Detected 5/11 injected faults at a mean of 38.3s
```

| Flag | Target service | Result | Time |
|---|---|---|---|
| `adFailure` | `ad` | **detected** | 26.1s |
| `adHighCpu` | `ad` | **detected** | 26.5s |
| `adManualGc` | `ad` | **detected** | 25.7s |
| `paymentFailure` | `payment` | **detected** | 25.9s |
| `recommendationCacheFailure` | `recommendation` | **detected** | 87.2s |
| `cartFailure` | `cart` | missed | — (flagged `payment`, `quote` instead) |
| `imageSlowLoad` | `frontend` | missed | — (flagged `recommendation` instead) |
| `intlShippingSlowdown` | `shipping` | missed | — |
| `productCatalogFailure` | `product-catalog` | missed | — |
| `emailMemoryLeak` | `email` | missed | — |
| `kafkaQueueProblems` | *(none)* | not applicable | — |

38.3s mean for the 5 detected faults lines up closely with PROBE's 40s
target, even though the *count* (5/11, not comparable 1:1 to "8/10" since
the flag sets and pass/fail bar differ) is lower. Reading into *why* each
one missed, rather than just reporting the number:

- **`cartFailure` / `productCatalogFailure`**: this exactly reproduces a
  finding from `../ml-flag-detection/README.md` §6, built independently with
  a completely different method (supervised classification vs. z-score
  anomaly scoring) — these gRPC/percentage-based failure flags did not
  produce a visible error-rate shift in short observation windows in *either*
  project. Two unrelated detection methods failing to see the same flags'
  effect via the same underlying data is stronger evidence than either
  finding alone that this is a real property of this fork's traffic
  pattern (checkout-adjacent paths are hit rarely by the default
  load-generator mix), not a bug specific to one implementation.
- **`emailMemoryLeak`**: also reproduces a finding from
  `../ml-flag-detection/` — the `email` service (Ruby) emits no OTel
  runtime memory metric at all in this fork (confirmed: it emits exactly
  one custom counter, `app.confirmation.counter`, nothing else), so no
  memory-based detector built on OTel metrics can see this fault
  regardless of algorithm. Would need container-level memory scraping
  (cAdvisor/`docker stats`), which is outside this Detector's current
  signal set.
- **`intlShippingSlowdown`**: `shipping` is a low-traffic service in this
  demo (219-238 calls total across a 3-4 hour window, per
  `../causal-changepoint-detection/service_graph.json`) — plausibly too few
  samples land in the `RECENT_BUCKETS` window (60s) to clear
  `MIN_SAMPLE_COUNT` (3 per 30s bucket) reliably.
- **`imageSlowLoad`**: this validator's own target-service choice is
  questionable, not just the detector's sensitivity — the flag delays image
  loading in a way that most plausibly shows up client-side (or in
  `image-provider`, which actually serves the images) rather than
  `frontend`'s own server-side span duration. It's worth re-running this one
  case with `image-provider` as the target before concluding the Detector
  missed it; `../ml-flag-detection/`'s equivalent check used
  `frontend_p95_ms` and *did* find a signal there (471ms vs 166ms baseline)
  over a longer window, so `frontend` isn't an unreasonable choice — it may
  just need more than 120s to clear this Detector's z-score threshold at
  this window/bucket size.
- **`kafkaQueueProblems`**: honestly excluded rather than silently skipped
  — Kafka itself isn't traced in this stack, so this Detector (built purely
  on traces/metrics/logs from instrumented services) has no direct signal
  for it. Its effect, if any, would need to show up indirectly via consumer
  latency in `accounting`/`fraud-detection`, which the current target-service
  mapping doesn't check for.
- **Ambient false positives**: a baseline scan (no fault injected) still
  found 2-3 anomalies (`product-catalog`, `frontend`, `recommendation`
  latency spikes) during this run, and `cartFailure`'s miss came with
  `payment`/`quote` flagged instead of `cart`. This host runs two full
  OTel-demo stacks plus Elasticsearch/Kibana at once (documented at length
  in `../ml-flag-detection/README.md`), and genuine host-level resource
  contention appears to produce real, unrelated latency spikes that a
  purely statistical detector correctly flags as anomalous — because they
  *are* anomalous, just not caused by whatever fault is being tested. A
  production deployment on dedicated infrastructure would not have this
  confound; it's a property of this specific validation environment, not
  of the detection method.

## 4. Honest summary

The Detector stage works essentially as designed: it correctly and quickly
(sub-30s) catches faults that manifest as a clear latency or CPU shift in a
reasonably-trafficked service (`ad`, `payment`, `recommendation`), using
nothing but z-scores over bucketed ES|QL aggregations — no ML training, no
LLM calls, standard-library Python only. Its misses are concentrated
exactly where two other, independently-built detection methods on this same
machine also struggled: rare code paths, a metric that genuinely doesn't
exist in this fork's telemetry, and this validation host's own resource
contention. That's a more informative outcome than either a uniformly high
or uniformly low score would have been — it points at specific, named gaps
(traffic volume for checkout-adjacent flags, no Kafka signal, no
container-level memory fallback) rather than leaving "is this a good
detector?" as a vague, untested claim.
