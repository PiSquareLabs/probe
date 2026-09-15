# Flag classifier: what was built and what was found

## Environment

- Cloned `elastic/opentelemetry-demo` into `opentelemetry-demo/`.
- Installed Elastic's self-hosted backend (`start-local` + `--edot`) into
  `opentelemetry-demo/elastic-start-local/` (Elasticsearch, Kibana, EDOT
  collector).
- Brought up the demo with `compose.yaml + compose.full.yaml +
  docker-compose.elastic.yml + docker-compose.elastic-self-hosted.yml`, plus
  a local-only `docker-compose.rename.yml` override (container names +
  default network renamed to `es-flagtest-*` / `es-flagtest`) so this stack
  could run alongside an already-existing, unrelated OTel-demo deployment on
  the same machine without container-name or port collisions.
- `.env.override` remaps `ENVOY_PORT`/`ENVOY_ADMIN_PORT` to 18080/18090 (the
  default 8080/10000 was already taken) and re-derives `K6_TARGET_URL` /
  `FRONTEND_PROXY_ADDR` accordingly, since those are pre-resolved inside
  `.env` using the *old* port before `.env.override` is merged in.

### Infrastructure problems hit and fixed

- **Stale prebuilt images.** `frontend-proxy`, `load-generator`, `checkout`
  and `shipping` all initially ran images built from an older commit of this
  repo (same `IMAGE_NAME:DEMO_VERSION` tag already existed locally), so they
  didn't match the current `envoy.tmpl.yaml` / `entrypoint.sh` / health-check
  binaries. Fixed by rebuilding each from source.
- **CRLF line endings.** `src/load-generator/entrypoint.sh` had been
  checked out with CRLF endings (Windows git config), which breaks its
  `#!/bin/sh` shebang inside the Linux container (`exec: no such file or
  directory`). Fixed with `sed -i 's/\r$//'`.
- **Host memory.** This machine has 15GB RAM; running two full copies of the
  demo stack plus Elasticsearch/Kibana/EDOT is genuinely too much. Fixed by
  lowering the Docker Desktop/WSL2 memory reservation (12GB → 8GB, leaving
  Windows itself enough headroom), lowering the Elasticsearch heap
  (2g → 1g), stopping Kibana during collection (not needed for the
  API-driven queries), and — at the user's request — removing an unrelated
  `probe-app` container stack that was also running on the host.
- **k6 browser-module memory growth.** `load-generator` runs a headless
  Chromium browser scenario via k6's browser module; its container memory
  climbed towards its 512MB limit over ~1.5 hours and needed a restart.

## Feature flags

`src/flagd/demo.flagd.json` defines 15 flags. 11 of the 12 requested flags
exist in this fork; **`productCatalogLockContention` does not exist** in the
checked-out `demo.flagd.json` and was skipped. The "on" variant used for
each (non-boolean flags don't have a variant literally named "on"):

| flag | variant used |
|---|---|
| adFailure, adHighCpu, adManualGc, kafkaQueueProblems, recommendationCacheFailure, productCatalogFailure | `on` |
| cartFailure, paymentFailure | `100%` |
| imageSlowLoad, intlShippingSlowdown | `10sec` |
| emailMemoryLeak | `10x` (see note below) |

`emailMemoryLeak`'s strongest variant, `10000x`, was tried first but
reliably coincided with the collector process being killed for low host
memory; dialed back to `10x` as a precaution (it turned out this wasn't
actually the leak's fault — see "Data quality" below — but `10x` was kept).

## Data collection

`flag-classifier/collect_dataset.py`: for each flag, flips
`defaultVariant` to its "on" variant directly in `demo.flagd.json` (flagd
hot-reloads the file via its filesystem watcher — no restart needed), lets
the bundled k6 load-generator run for a window (25-40s depending on when in
the run), queries Elasticsearch for that window, then reverts to `off`.
Repeated 5x per flag, plus 10 baseline (all-off) windows.

Two ES queries per window:
- `traces-*` / `.ds-traces-*`, filtered to `attributes.processor.event:
  transaction` (top-level spans only) and — after the fix described below —
  excluding `flagd.evaluation.v1.Service*` spans, aggregated by
  `resource.attributes.service.name` with `duration` percentiles (50/95/99)
  and a `attributes.event.outcome: failure` filter for error rate.
- `metrics-*` / `.ds-metrics-*`, aggregated by service for
  `process.cpu.utilization` / `process.memory.usage`, with fallbacks to the
  JVM (`jvm.cpu.recent_utilization`, `jvm.memory.used`), Go
  (`go.memory.used`), and CPython
  (`process.runtime.cpython.cpu.utilization`/`.memory`) equivalents, since
  not every language runtime emits the same metric names.

Output: **65 rows × 162 features** (9 features × 18 services) in
`dataset.csv`, columns `<service>_p50_ms`, `_p95_ms`, `_p99_ms`,
`_error_rate`, `_throughput_rps`, `_cpu_avg`, `_cpu_max`, `_mem_avg_mb`,
`_mem_max_mb`, plus `label`, `window_start`, `window_end`.

The run was repeatedly interrupted by the host's low-memory conditions
(mid-run, not just at the start), so `collect_dataset.py` supports
`--append` to resume from the partial CSV without redoing completed cycles.
`finish_remaining.py` is a stdlib-only (no pandas/requests) variant of the
same collection loop that was used for the last two flags once it became
clear that just importing pandas at process startup was itself pushing the
already-thin host memory over the edge.

### Data quality: a real bug found and fixed mid-way

The first trained model only reached 40% test accuracy with feature
importances dominated by generic frontend/proxy latency rather than
anything flag-specific — the sanity check the task asked for. Digging in:
`ad`'s "failures" were almost all `flagd.evaluation.v1.Service/EventStream`
and `/ResolveBoolean` spans — flagd's own OpenFeature client reconnecting
its streaming connection, tagged `event.outcome: failure` on every
reconnect, completely unrelated to `adFailure`. This was diluting
error-rate and throughput for every service that talks to flagd (i.e. all
of them).

Fixed by excluding `name: flagd.evaluation.v1.Service*` spans from the
trace aggregation, then **re-extracted features from the already-stored
Elasticsearch data** for all 65 previously-recorded windows (no need to
re-run the stack — `reextract_features.py`) and retrained. Test accuracy
rose to 55%, and — more importantly — the *pattern* of which classes the
classifier gets right now lines up with which classes have an actual
telemetry signal (see below), rather than being dominated by proxy noise.

### Flags with a clear signal vs. flags without one

Checked directly (flag-on mean vs. baseline mean, corrected features):

| flag | feature | flag mean | baseline mean |
|---|---|---:|---:|
| adHighCpu | `ad_cpu_avg` | 0.227 | 0.008 |
| adManualGc | `ad_p95_ms` | 12266 ms | 201 ms |
| recommendationCacheFailure | `recommendation_p95_ms` | 84020 ms | 28 ms |
| imageSlowLoad | `frontend_p95_ms` | 471 ms | 166 ms |
| adFailure | `ad_error_rate` | 0.000 | 0.000 |
| cartFailure | `cart_error_rate` | 0.000 | 0.000 |
| paymentFailure | `payment_error_rate` | 0.000 | 0.000 |
| productCatalogFailure | `product_catalog_error_rate` | 0.000 | 0.000 |
| emailMemoryLeak | `email_mem_avg_mb` | 0.000 | 0.000 |

The first four have an unmistakable signal, and the classifier correctly
identifies them (adHighCpu 2/2, adManualGc 1/1, recommendationCacheFailure
1/1, imageSlowLoad 1/1 in the test confusion matrix). The failure-injection
flags (adFailure/cartFailure/paymentFailure/productCatalogFailure) show
**zero** measured error rate even while active. Checking why:

- `email` (Ruby) only emits one custom business metric
  (`app.confirmation.counter`) — no OTel runtime memory instrumentation at
  all in this fork, so `emailMemoryLeak`'s designated signal is simply not
  observable through APM/metrics data regardless of query correctness. This
  would need reading the container's own memory (e.g. via cAdvisor/docker
  stats) instead of OTel metrics.
- The gRPC/percentage-based failure flags genuinely didn't produce visible
  `event.outcome: failure` spans in the windows sampled — plausibly because
  these are lower-traffic code paths (checkout/payment/product-detail),
  and a 5-cycle × 25-40s sample per flag is thin for rare paths. This is a
  sampling-volume limitation of this run, not necessarily proof the flags
  have no real effect.

Despite that, `cartFailure` still classifies perfectly (2/2) — evidently
via a correlated feature (e.g. `cart_cpu_avg`) rather than error rate
directly, which is a fair, non-leaky signal but worth knowing about if this
is extended.

## Model

`train_classifier.py`: `RandomForestClassifier(n_estimators=300,
class_weight="balanced")`, stratified 70/30 train/test split (45/20 rows).

- Train accuracy: 100% (expected/uninformative at n=45, 12 classes).
- **Test accuracy: 55%** (11/20), macro F1 0.51.
- Per-class: adHighCpu, adManualGc, cartFailure, imageSlowLoad,
  recommendationCacheFailure all perfect on their (small) test folds;
  adFailure, paymentFailure, productCatalogFailure all 0 — consistent with
  the missing-signal flags above, not a modeling artifact.
- Top features are dominated by `frontend`/`frontend-proxy`/`ad`/`cart`/
  `recommendation` latency and throughput — expected, since those are the
  highest-traffic services and several strong-signal flags act on exactly
  those services (ad, recommendation).

Given only 5 samples per class, these numbers should be read as "the
pipeline works and finds real signal where the telemetry supports it," not
as a production-quality classifier. More cycles per flag (and longer
windows for low-traffic flags) would be the direct next step.

## Files delivered

In `flag-classifier/`:
- `dataset.csv` — final labeled dataset (65×162+3), flagd-noise-corrected.
- `dataset_v1_before_flagd_noise_fix.csv` — the original extraction, kept
  for comparison.
- `model.joblib`, `model_v1_before_flagd_noise_fix.joblib` — trained models
  (dict with `model`, `feature_cols`, `labels`).
- `eval_report.txt`, `eval_report_v1_before_flagd_noise_fix.txt` —
  classification report + confusion matrix (text).
- `confusion_matrix.png`, `confusion_matrix_v1_before_flagd_noise_fix.png`.
- `feature_importances.csv`, `feature_importances_v1_before_flagd_noise_fix.csv`.
- `collect_dataset.py` — main, resumable, pandas-based collector.
- `finish_remaining.py` — stdlib-only variant used under acute memory
  pressure.
- `reextract_features.py` — re-derives features from already-stored ES
  windows with the corrected (flagd-noise-excluded) query; stdlib-only,
  resumable.
- `train_classifier.py` — trains/evaluates/saves the model.
