# ML Flag Detection

**Detection method:** supervised multi-class classification (scikit-learn
`RandomForestClassifier`) that predicts *which* flagd feature flag is
currently active in the Elastic OpenTelemetry demo, purely from an
aggregated window of per-service telemetry (Elasticsearch/APM traces +
metrics) — no direct access to the flag state itself.

This is the ML counterpart to rule-based/threshold anomaly detection: instead
of hand-writing "if ad p95 > X ms, suspect adManualGc," a model is trained on
labeled telemetry windows (label = which flag was on, or `baseline`) and
learns which combination of per-service latency/error/throughput/CPU/memory
features distinguishes each fault from normal operation and from every other
fault.

---

## 1. How it works, end to end

```
 ┌─────────────────┐   1. flip flag    ┌──────────────────────────┐
 │ demo.flagd.json  │ ─────────────────▶│ flagd (hot file-watch    │
 │ (defaultVariant) │                   │ reload, no restart)      │
 └─────────────────┘                   └────────────┬─────────────┘
                                                      │ affects behaviour of
                                                      ▼
                                        ┌──────────────────────────┐
        2. k6 load-generator  ────────▶ │ OTel demo microservices  │
        (already bundled, runs          │ (ad, cart, checkout, …)  │
         continuously)                  └────────────┬─────────────┘
                                                      │ OTLP traces + metrics
                                                      ▼
                                        ┌──────────────────────────┐
                                        │ EDOT collector →          │
                                        │ Elasticsearch (traces-*,  │
                                        │ metrics-*, .ds-* streams) │
                                        └────────────┬─────────────┘
                                                      │ 3. aggregation query
                                                      │    per service, per window
                                                      ▼
                                        ┌──────────────────────────┐
                                        │ one CSV row: label +      │
                                        │ 9 features × 18 services  │
                                        └────────────┬─────────────┘
                                                      │ repeat ~5x per flag
                                                      │ + baseline (all off)
                                                      ▼
                                        ┌──────────────────────────┐
                                        │ RandomForestClassifier    │
                                        │ train/test, eval, save    │
                                        └──────────────────────────┘
```

1. **Flag toggling** — `demo.flagd.json`'s `defaultVariant` for one flag is
   rewritten directly on disk. flagd watches this file and hot-reloads it
   (no container restart needed).
2. **Load generation** — the demo's own bundled k6 load-generator keeps
   hitting the frontend continuously; no separate driver is needed.
3. **Telemetry window** — after a short settle period, the script sleeps for
   the observation window (25–40s depending on collection run), records the
   `[start, end)` timestamps, then reverts the flag to `off`.
4. **Feature extraction** — two Elasticsearch aggregation queries turn the
   raw traces/metrics for that window into one row: for each of 18 services,
   `p50_ms`, `p95_ms`, `p99_ms`, `error_rate`, `throughput_rps` (from traces)
   and `cpu_avg`, `cpu_max`, `mem_avg_mb`, `mem_max_mb` (from metrics) — 162
   feature columns total, labeled with which flag (or `baseline`) was active.
5. **Training** — a `RandomForestClassifier` is trained on a stratified
   train/test split of the resulting rows, evaluated with a classification
   report and confusion matrix, and its feature importances are sanity
   checked against domain expectations (e.g. does `adFailure` actually key
   off `ad_error_rate`?).

## 2. Environment this was built against

- Elastic's OpenTelemetry demo fork (`elastic/opentelemetry-demo`), cloned
  into `../opentelemetry-demo/` (sibling of this folder).
- Brought up via `compose.yaml + compose.full.yaml +
  docker-compose.elastic.yml + docker-compose.elastic-self-hosted.yml`, with
  a local-only `docker-compose.rename.yml` overlay (see below) — not part of
  upstream, kept only in the local checkout.
- Backend: Elastic's `start-local` installer (`curl -fsSL
  https://elastic.co/start-local | sh -s -- --edot`) run inside
  `../opentelemetry-demo/`, which creates
  `../opentelemetry-demo/elastic-start-local/` (Elasticsearch, Kibana, the
  EDOT collector, and the credentials in its `.env`).

### Why `docker-compose.rename.yml` exists

This host already had a *different* checkout of the same demo running
(unrelated to this task). `compose.yaml` hardcodes `container_name:` for
every service and a fixed default network name (`opentelemetry-demo`), so a
second checkout can't come up side by side without collisions. The override
renames every container to `es-flagtest-<service>` and the network to
`es-flagtest`. **If you're running this on a clean host with no other demo
instance, you don't need it** — just drop `-f docker-compose.rename.yml`
from the compose command.

### Port/env overrides (`.env.override`)

```
ENVOY_PORT=18080
ENVOY_ADMIN_PORT=18090
FRONTEND_PROXY_ADDR=frontend-proxy:18080
K6_TARGET_URL=http://frontend-proxy:18080
```

Also only needed because port 8080 was already taken on this host by the
other instance. `FRONTEND_PROXY_ADDR`/`K6_TARGET_URL` must be re-derived
explicitly because `.env` resolves them from `${ENVOY_PORT}` *before*
`.env.override` is merged in — overriding `ENVOY_PORT` alone silently leaves
the load-generator pointed at the old port.

### Bringing the stack up

```bash
cd ../opentelemetry-demo
docker compose --env-file .env --env-file .env.override \
  -f compose.yaml -f compose.full.yaml \
  -f docker-compose.elastic.yml -f docker-compose.elastic-self-hosted.yml \
  -f docker-compose.rename.yml \
  up -d
```

`compose.full.yaml` is required even though it looks optional — it's what
actually defines the `kafka`/`accounting`/`fraud-detection` services (needed
for the `kafkaQueueProblems` flag); the base `compose.yaml` doesn't include
them.

## 3. Running the pipeline

```bash
pip install requests scikit-learn pandas numpy matplotlib joblib

# 1. Collect the dataset (≈65 rows takes ~40-70 min depending on host speed)
python collect_dataset.py --out dataset.csv

# 2. Train + evaluate
python train_classifier.py --data dataset.csv
```

`collect_dataset.py` is resumable: pass `--append` and it will read the
existing CSV, figure out which (flag, cycle) combinations are already done,
and continue from there instead of starting over — useful because a 60+
minute run against a live Docker stack can get interrupted.

If the host is memory-constrained enough that even *starting a new Python
process* (importing pandas/numpy) risks getting killed, use
`finish_remaining.py` instead — a stdlib-only (`urllib` + `csv`, no
pandas/requests) equivalent for finishing the last few flags one cycle at a
time:

```bash
python finish_remaining.py dataset.csv <flagName> <variant> <nCycles>
# e.g.
python finish_remaining.py dataset.csv productCatalogFailure on 5
```

`reextract_features.py` re-runs the feature-extraction queries against
Elasticsearch for windows that are *already recorded* in `dataset.csv`
(reading only `window_start`/`window_end` + `label`, no need for the demo
stack to still be generating traffic) — used here to fix a bug retroactively
without re-running the whole collection (see §5).

### 3a. Live prediction with shared result output

The trained model only ever saw trace+metric features (§1) — adding logs
as a *training* feature would need a full re-collection pass. Instead,
`predict_and_bundle.py` scores one live window against the existing model
and attaches two things the model itself doesn't produce: a live,
non-training-feature log-error check over the same window, and the
service dependency graph (`service_graph.json`, copied from
`../causal-changepoint-detection/`'s empirically-derived graph):

```bash
python predict_and_bundle.py --model model.joblib --out result.json
```

Schema:

```json
{
  "detector": "ml-flag-detection",
  "generated_at": "...",
  "predicted_label": "imageSlowLoad",
  "class_probabilities": {"imageSlowLoad": 0.22, "...": ...},
  "log_error_summary": [{"service.name": "...", "error_count": N}],
  "service_dependency_graph": {"edges": [{"caller": "...", "callee": "...", "count": N}]},
  "named_root_cause": "frontend"
}
```

`named_root_cause` is the predicted flag's target service (via the same
flag→service map every other detector's validator uses), or `null` when
the prediction is `"baseline"`. Given §6's 55% test accuracy, treat a
single prediction as a hint, not a verdict — this is most useful run
repeatedly over a live incident, not as a one-shot answer. This module
only *produces* the result file; it does not call, know about, or depend
on any Remediator.

## 4. File reference

| File | What it is |
|---|---|
| `collect_dataset.py` | Main collector: toggles flags, samples ES, writes `dataset.csv`. Resumable via `--append`. |
| `finish_remaining.py` | stdlib-only fallback collector for one flag/variant/N-cycles at a time, for memory-constrained hosts. |
| `reextract_features.py` | Re-derives features from already-recorded ES windows with a corrected query, without re-running the stack. Resumable. |
| `predict_and_bundle.py` | Scores one live window against the trained model, bundles a live log-error check + the service dependency graph, writes a shareable result JSON (see §3a). |
| `service_graph.json` | Copy of `../causal-changepoint-detection/service_graph.json` — same demo stack, same graph. |
| `train_classifier.py` | Trains/evaluates the RandomForest, saves model + report + confusion matrix + feature importances. |
| `dataset.csv` | **Final, corrected** labeled dataset — 65 rows × (162 features + `label` + `window_start` + `window_end`). |
| `model.joblib` | Trained model: `joblib.load(...)` returns `{"model": RandomForestClassifier, "feature_cols": [...], "labels": [...]}`. |
| `eval_report.txt` | Classification report + confusion matrix (plain text). |
| `confusion_matrix.png` | Same confusion matrix, plotted. |
| `feature_importances.csv` | Every feature's Gini importance, sorted descending. |
| `dataset_v1_before_flagd_noise_fix.csv`, `model_v1_before_flagd_noise_fix.joblib`, `eval_report_v1_before_flagd_noise_fix.txt`, `confusion_matrix_v1_before_flagd_noise_fix.png`, `feature_importances_v1_before_flagd_noise_fix.csv` | The *first* extraction/training pass, before the flagd-noise bug (§5) was found and fixed. Kept only so the before/after diagnostic in §5 is verifiable; not the intended deliverable. |
| `SUMMARY.md` | Narrative write-up covering the same ground as this README with more infra-debugging detail (stale Docker images, CRLF line endings, host memory tuning). Read this if you want the full "what broke and how it was fixed" story. |

## 5. A bug this pipeline caught in itself (worth knowing about)

The first trained model reached only 40% test accuracy, with feature
importances dominated by generic `frontend`/`frontend-proxy` latency rather
than anything flag-specific — exactly the kind of thing a feature-importance
sanity check is supposed to catch. Investigating: nearly every service's
"failure" spans in Elasticsearch turned out to be
`flagd.evaluation.v1.Service/EventStream` and `/ResolveBoolean` —
flagd's own OpenFeature client reconnecting its streaming connection,
tagged `event.outcome: failure` on every reconnect. That's constant
background noise unrelated to any injected fault, and it was diluting
error-rate and latency for every service that talks to flagd (all of them).

**Fix:** exclude `name: flagd.evaluation.v1.Service*` spans from the trace
aggregation (see `query_trace_features` in `reextract_features.py`), then
re-extract features for the already-recorded windows and retrain. Test
accuracy rose to 55%, and — more importantly — the *pattern* of which
classes the model gets right lines up with which flags have a real,
independently-verifiable telemetry signal (§6), instead of being an
artifact of proxy noise.

**Lesson for anyone extending this:** any service that resolves flags via
flagd's streaming API will show this noise in `traces-*`; filter it out
before computing per-service error rates or throughput.

## 6. Results

`RandomForestClassifier(n_estimators=300, class_weight="balanced")`,
stratified 70/30 split (45 train / 20 test rows, 12 classes: 11 flags +
baseline).

- **Test accuracy: 55%** (11/20). Train accuracy 100% (expected/uninformative
  at 45 rows over 12 classes — not a claim of a well-generalizing model).
- Perfect or near-perfect on the test fold: `adHighCpu`, `adManualGc`,
  `cartFailure`, `imageSlowLoad`, `recommendationCacheFailure`.
- Zero on the test fold: `adFailure`, `paymentFailure`,
  `productCatalogFailure` — see below, this tracks a real telemetry gap, not
  a modeling failure.

Directly checking flag-on mean vs. baseline mean for the feature each flag
should most obviously affect:

| flag | feature | flag mean | baseline mean | verdict |
|---|---|---:|---:|---|
| `adHighCpu` | `ad_cpu_avg` | 0.227 | 0.008 | strong signal |
| `adManualGc` | `ad_p95_ms` | 12266 ms | 201 ms | strong signal |
| `recommendationCacheFailure` | `recommendation_p95_ms` | 84020 ms | 28 ms | strong signal |
| `imageSlowLoad` | `frontend_p95_ms` | 471 ms | 166 ms | strong signal |
| `adFailure` | `ad_error_rate` | 0.000 | 0.000 | **no signal observed** |
| `cartFailure` | `cart_error_rate` | 0.000 | 0.000 | no signal on this feature (classifies via `cart_cpu_avg` instead — see caveat) |
| `paymentFailure` | `payment_error_rate` | 0.000 | 0.000 | **no signal observed** |
| `productCatalogFailure` | `product_catalog_error_rate` | 0.000 | 0.000 | **no signal observed** |
| `emailMemoryLeak` | `email_mem_avg_mb` | 0.000 | 0.000 | **not observable at all** (see below) |

Why the "no signal" rows: (a) `email` (Ruby) in this fork only emits one
custom business counter (`app.confirmation.counter`) — no OTel runtime
memory instrumentation whatsoever, so `emailMemoryLeak`'s designated signal
cannot be seen through APM/metrics data no matter how the query is written;
it would need container-level memory (e.g. cAdvisor/`docker stats`) instead.
(b) the percentage/gRPC failure flags (`adFailure`, `cartFailure`,
`paymentFailure`, `productCatalogFailure`) didn't produce any visible
`event.outcome: failure` spans in the 5×25-40s windows sampled for
them — plausibly because these hit lower-traffic code paths
(checkout/payment/product-detail pages), so 5 short cycles is a thin sample
for a rare path. This is a **sampling volume limitation of this run**, not
proof the flags have zero effect — more cycles and/or longer windows for
these specific flags would be the direct next step to confirm either way.

`cartFailure` is a useful caution here: it still classifies perfectly (2/2)
despite zero measured error rate, evidently via a correlated feature
(`cart_cpu_avg`) instead. That's a legitimate, non-leaky signal (cart really
does behave differently when the flag is on), but it's a reminder that
"the model got the class right" doesn't always mean "for the reason you'd
expect" — check the per-class feature-importance breakdown before trusting
a specific explanation.

## 7. Known limitations / next steps

- **Small sample size.** 5 cycles per flag is enough to prove the pipeline
  works end-to-end and to catch the flagd-noise bug, but not enough for a
  robust classifier. 15-20+ cycles per flag would substantially firm up the
  weaker classes.
- **Fixed window length may miss low-frequency flags.** `adFailure` /
  `paymentFailure` / `productCatalogFailure` / `cartFailure` affect
  checkout-adjacent paths that the load-generator hits relatively rarely;
  longer windows (or a load-generator scenario weighted toward checkout)
  would give the error-rate feature more chances to see the effect.
  `kafkaQueueProblems` similarly has no direct Kafka broker metrics in the
  feature set (Kafka isn't traced) — its signal, if any, is currently only
  indirect (via consumer latency in `accounting`/`fraud-detection`).
- **`emailMemoryLeak` needs a different data source.** OTel metrics won't
  show it in this fork; would need `docker stats`/cAdvisor-style
  container-memory scraping instead.
- **Different window durations across the run.** The first ~9 flags were
  collected with 40s windows and the last two with 25s windows (a mid-run
  adjustment made after resource-constraint troubleshooting) — `throughput_rps`
  is normalized per-second so this shouldn't bias that feature directly, but
  it's worth knowing if you see it and wonder why.
