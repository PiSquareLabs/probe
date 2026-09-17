# Elastic ML Anomaly Detection

**Detection method:** Elastic's native, built-in ML anomaly detection jobs
(unsupervised, time-series based) — the fourth detection method against
this repo's demo stack, built via the `elasticsearch-anomaly-detection`
Claude skill (`.claude/skills/elasticsearch-anomaly-detection/`) rather
than hand-written from scratch like the other three.

Where `causal-changepoint-detection` and `probe-detector` implement their
own statistics (ES|QL `CHANGE_POINT`, client-side z-scores) against raw
Elasticsearch queries, this uses Elasticsearch's own ML subsystem end to
end: a persistent, continuously-learning model per job, served through the
`_ml/anomaly_detectors` API.

## 1. Setup (via the `elastic` CLI, per the skill's required order)

The skill is explicit that this must go through the [`elastic`
CLI](https://github.com/elastic/cli), not raw HTTP calls — installed here
with `npm install -g @elastic/cli` and pointed at the local self-hosted
cluster:

```bash
elastic config context add local-otel-demo \
  --es-url "http://localhost:9200" --es-username elastic --es-password "$ES_LOCAL_PASSWORD" \
  --kb-url "http://localhost:5601" --kb-username elastic --kb-password "$ES_LOCAL_PASSWORD"
elastic config current-context set local-otel-demo
elastic status   # confirms connectivity before doing anything else
```

Two jobs, created in the skill's mandatory order (job → datafeed → open
job → start datafeed) — see `jobs/` for the exact request bodies used:

| Job | Detector | Field | Partition |
|---|---|---|---|
| `otel-demo-latency` | `high_mean` | `duration` (span duration, ns) | `resource.attributes.service.name` |
| `otel-demo-error-rate` | `high_count` | *(count of matching docs)* | `resource.attributes.service.name` |

Both use `bucket_span: "5m"` (Elastic ML's typical default granularity —
see §3 for why this matters a lot here) and a datafeed query that excludes
`flagd.evaluation.v1.Service*` spans — the same noise source
`ml-flag-detection`, `causal-changepoint-detection`, and `probe-detector`
each independently found and fixed. `otel-demo-error-rate`'s datafeed also
filters to `attributes.event.outcome: failure` so `high_count` counts
failed spans specifically, not all traffic.

```bash
python -c "..."  # see jobs/*.json for the exact bodies
elastic es ml put-job --input-file jobs/job-latency.json
elastic es ml put-datafeed --input-file jobs/datafeed-latency.json
elastic es ml open-job --job-id otel-demo-latency
elastic es ml start-datafeed --datafeed-id datafeed-otel-demo-latency
# ...repeat for otel-demo-error-rate
```

Verify with `elastic es ml get-job-stats` / `get-datafeed-stats` — the
skill is explicit that "created" is not "opened" and "started"; report
both states, not just successful creation.

### A behavior worth knowing: datafeeds backfill immediately

Starting a datafeed with no explicit `start` time doesn't begin from "now"
— it immediately processes **all** matching historical documents in the
index, as fast as it can read them, then transitions to real-time. Both
jobs here ingested 635+ five-minute buckets (~53 hours of this stack's
entire trace history) within seconds of starting. This turned out to be
useful: every fault this repo has ever injected during earlier detector
work was already sitting in the index, so validating against it didn't
require re-injecting anything live (§3) — but it's a surprising default if
you expect a job to start "quiet."

## 2. Files

| File | What it is |
|---|---|
| `jobs/job-latency.json`, `jobs/datafeed-latency.json` | Request bodies for the latency job + its datafeed. |
| `jobs/job-error-rate.json`, `jobs/datafeed-error-rate.json` | Same for the error-rate job. |
| `validate_against_history.py` | Cross-references every fault window already recorded in `../ml-flag-detection/dataset.csv` against these jobs' anomaly records — no live re-injection needed (§3). |
| `validation_results.json` | Output of the above. |
| `scan_current.py` | Live one-shot scan: recent anomaly records from both jobs, bundled with the service dependency graph into this repo's shared result schema (`--out result.json`), same as the other three detectors. |
| `service_graph.json` | Copy of `../causal-changepoint-detection/service_graph.json` — same demo stack, same graph. |

## 3. Validation: cross-referenced against 55 known fault windows

`../ml-flag-detection/dataset.csv` already has exact `window_start`/
`window_end`/`label` for 65 telemetry windows (10 baseline + 5 cycles ×
11 flags) recorded while building that project. Since both ML jobs had
already backfilled through that entire period, `validate_against_history.py`
just asks: for each non-baseline window, is there an anomaly record
(`record_score >= 10`) for that flag's target service within
`[window_start, window_end + 5 minutes]` (one bucket of slack)?

```
Detected in at least one window: 0/11 flags
```

Every one of the 55 fault windows scored **exactly 0.0** — not "below
threshold," genuinely zero, confirmed two ways: individual anomaly
records (`result_type: record`) and the coarser per-bucket score
(`result_type: bucket`) both read 0 across the buckets containing, e.g.,
`adManualGc`'s injections (the flag with the single strongest signal found
by every other detector in this repo — 12,000ms+ p95 latency vs. ~200ms
baseline in `causal-changepoint-detection`'s testing).

**Why, concretely:** `ml-flag-detection`'s collection windows are 25-40
seconds long. Both ML jobs use a 5-minute (300s) `bucket_span`. A 30-second
fault folded into a `high_mean` average over 300 seconds of otherwise
normal traffic gets diluted by roughly 10x before the model ever sees it —
even a fault as extreme as a 60x latency spike can land back inside the
model's learned confidence bounds once averaged down like that. This
isn't a configuration mistake to fix by lowering the threshold; the
bucket itself doesn't contain enough signal to score highly regardless of
threshold, as the zero *bucket*-level score (not just record-level)
confirms.

**This is a fair, if unflattering, result — not a broken setup.** Elastic
ML anomaly detection is built for sustained degradations measured in
minutes-to-hours (a slow memory leak, a gradually saturating queue, a
real-world incident that unfolds over time), not a 30-second synthetic
test. `bucket_span` could be shortened, but Elastic's own guidance treats
sub-minute bucket spans as unusual and generally recommends against very
short spans since the model needs enough buckets to learn any
seasonality; a fairer test of this method would inject each fault for
10-15+ minutes (several full buckets) rather than 30 seconds — **not done
here** due to the time cost (11 flags × 15+ minutes each ≈ 3+ hours), and
noted honestly as the direct next step rather than glossed over.

## 4. Honest comparison to this repo's other three detectors

| | `ml-flag-detection` | `causal-changepoint-detection` | `probe-detector` | Elastic ML (this) |
|---|---|---|---|---|
| Detection granularity | 25-40s windows | 20-30s buckets | continuous, ~10s polling | 5-minute buckets |
| Needs training data | Yes | No | No | No (learns online) |
| Detected on the same short-fault windows | 55% test accuracy | 5/11 | 5/11 | **0/11** |
| Built by | hand, this project | hand, this project | hand, this project | Elastic's own ML subsystem, via a Claude skill |

The other three were purpose-built around this repo's specific 25-40s
fault-injection cadence; Elastic ML's default bucket span was not, and the
gap is exactly as large as that mismatch predicts. This says more about
matching a tool's time granularity to the test than it says ML anomaly
detection doesn't work — see §3's honest-limitations note. The
`elasticsearch-anomaly-detection-explainer` skill (also installed in
`.claude/skills/`) is the right next step for interpreting *why* a given
score is high or low once a job with a properly-matched bucket span is
running against real, sustained production traffic.

## 5. Shared result output (for a downstream Correlator/Remediator)

`python scan_current.py --minutes 30 --out result.json` — same schema
convention as the other three detectors: anomalies plus the service
dependency graph, `named_root_cause` always `null` (no ranking layer
here, same reasoning as `probe-detector`). This module only *produces*
that file; it does not call, know about, or depend on any Remediator.

## 6. Teardown

```bash
elastic es ml stop-datafeed --datafeed-id datafeed-otel-demo-latency
elastic es ml close-job --job-id otel-demo-latency
elastic es ml stop-datafeed --datafeed-id datafeed-otel-demo-error-rate
elastic es ml close-job --job-id otel-demo-error-rate
```

Per the skill: stop the datafeed before closing the job, always in that
order.
