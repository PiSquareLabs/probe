# Running everything locally (self-hosted Elasticsearch, no cloud account)

This is the setup all five detectors in this repo were actually built and
validated against: the OpenTelemetry demo app, self-hosted Elasticsearch +
Kibana + EDOT via Elastic's `start-local` installer, and each detector
pointed at `localhost:9200`. See [`RUNNING_ON_ELASTIC_CLOUD.md`](RUNNING_ON_ELASTIC_CLOUD.md)
for the equivalent against a real Elastic Cloud/Serverless deployment
instead — the demo app and every detector script support both via the
same `ES_URL`/`ES_API_KEY` environment variables, switched at the bottom
of this doc's §3.

For the full narrative behind every decision here (why these exact compose
files, why the port remap, why two real Docker bugs needed fixing), see
[`DETECTORS.md`](DETECTORS.md) — this document is the condensed,
step-by-step version to actually run from.

## 1. Requirements

- **Docker Desktop**, with real headroom for it. Budget at least 8GB of
  RAM for Docker specifically. If you're on Windows and the host itself
  starts acting starved (not just Docker), check
  `C:\Users\<user>\.wslconfig`'s `memory=` setting — an over-allocated
  WSL2 VM was the actual root cause the one time this happened while
  building this repo (see `ml-flag-detection/README.md` for the full
  story). Changing `.wslconfig` requires `wsl --shutdown` + restarting
  Docker Desktop, which restarts *every* container on the host, not just
  this demo.
- **Python 3.11+**. A virtualenv is recommended; none was used originally
  (packages were installed globally per-detector — see §4 below).
- **~10GB free disk** for the demo images + Elasticsearch data.
- **`curl`** (for the `start-local` installer in §2).
- Optional: **Node.js** (`npm`) if you want the `elastic` CLI for the
  Elastic ML detector (§4d) — see that section for the install command.

## 2. Bring up the demo application

### 2a. Clone it and pull the submodule

```bash
git clone <this-repo-url>
cd <this-repo>
git submodule update --init          # pulls opentelemetry-demo/
```

`opentelemetry-demo/` is a git submodule pointing at
[`elastic/opentelemetry-demo`](https://github.com/elastic/opentelemetry-demo)
(Elastic's fork of the OpenTelemetry Astronomy Shop demo, using Elastic's
EDOT agents/collector) — not vendored directly into this repo.

### 2b. Install Elasticsearch + Kibana + EDOT (self-hosted, no cloud account)

```bash
cd opentelemetry-demo
curl -fsSL https://elastic.co/start-local | sh -s -- --edot
```

This creates `opentelemetry-demo/elastic-start-local/` — Elasticsearch,
Kibana, and an EDOT collector, plus a `.env` file with a generated
`ES_LOCAL_PASSWORD` that every detector's default (no-env-vars) mode reads
directly from that file to authenticate.

If Docker Desktop ever restarts, **these three containers do not
auto-restart** (no restart policy is set on them) — bring them back with:

```bash
cd opentelemetry-demo/elastic-start-local
docker compose --env-file .env -f docker-compose.yml up -d
```

### 2c. Bring up the demo's microservices

```bash
cd opentelemetry-demo
docker compose --env-file .env \
  -f compose.yaml -f compose.full.yaml \
  -f docker-compose.elastic.yml -f docker-compose.elastic-self-hosted.yml \
  up -d
```

- `compose.full.yaml` is **not optional** even though it reads like an
  add-on — it defines `kafka`/`accounting`/`fraud-detection`, which
  several flagd faults need.
- If another instance of this same demo is already running on your Docker
  host (container names collide — `compose.yaml` hardcodes
  `container_name:` per service), use the renaming/port overlay in
  `demo-overlay/` instead:
  ```bash
  cd opentelemetry-demo
  cp ../demo-overlay/.env.override ../demo-overlay/docker-compose.rename.yml .
  docker compose --env-file .env --env-file .env.override \
    -f compose.yaml -f compose.full.yaml \
    -f docker-compose.elastic.yml -f docker-compose.elastic-self-hosted.yml \
    -f docker-compose.rename.yml \
    up -d
  ```
  Edit the port numbers in `demo-overlay/.env.override` if 18080/18090
  are *also* taken on your host.

**Two real bugs to expect on a from-scratch bring-up** (both hit and
documented in depth in `ml-flag-detection/README.md`):

1. **Stale prebuilt images.** If any service's image already exists
   locally under the same `IMAGE_NAME:DEMO_VERSION` tag (e.g. from a
   previous checkout), Docker won't rebuild it automatically even though
   the source changed. `frontend-proxy`, `load-generator`, `checkout`,
   and `shipping` all needed an explicit rebuild here:
   ```bash
   docker compose -f compose.yaml -f compose.full.yaml -f docker-compose.elastic.yml -f docker-compose.elastic-self-hosted.yml build <service>
   ```
   Symptoms if you skip this: Envoy config validation errors on
   `frontend-proxy`, `wget: not found` or `exec: no such file or
   directory` on healthcheck binaries.
2. **CRLF line endings on Windows.** `src/load-generator/entrypoint.sh`
   can get checked out with CRLF endings, which breaks its `#!/bin/sh`
   shebang inside the Linux container. Fix, then rebuild that one
   service:
   ```bash
   sed -i 's/\r$//' opentelemetry-demo/src/load-generator/entrypoint.sh
   docker compose -f compose.yaml -f compose.full.yaml -f docker-compose.elastic.yml -f docker-compose.elastic-self-hosted.yml build load-generator
   ```

### 2d. Confirm it's working

```bash
docker ps --format "{{.Names}}\t{{.Status}}"
```

Everything should show `Up`/`healthy`, except `checkout`, `shipping`, and
`product-catalog`, which this fork's own healthchecks report as
`unhealthy` even when functioning correctly (a known quirk, not something
you introduced). The bundled k6 load-generator runs automatically once
`load-generator` is up — no separate step needed to generate traffic.

## 3. flagd fault injection (how every detector is tested)

Every detector in this repo is validated the same way: edit
`opentelemetry-demo/src/flagd/demo.flagd.json`, change one flag's
`defaultVariant` to its "on" value, wait, observe, then set it back to
`"off"`. flagd watches this file and hot-reloads it — **no container
restart needed**.

| Flag | "on" variant | Target service |
|---|---|---|
| `adFailure` | `on` | `ad` |
| `adHighCpu` | `on` | `ad` |
| `adManualGc` | `on` | `ad` |
| `cartFailure` | `100%` | `cart` |
| `paymentFailure` | `100%` | `payment` |
| `recommendationCacheFailure` | `on` | `recommendation` |
| `imageSlowLoad` | `10sec` | `frontend` |
| `intlShippingSlowdown` | `10sec` | `shipping` |
| `productCatalogFailure` | `on` | `product-catalog` |
| `emailMemoryLeak` | `10x`\* | `email` |
| `kafkaQueueProblems` | `on` | *(no direct trace/metric target — Kafka isn't traced)* |

\* `emailMemoryLeak`'s strongest variant is `10000x`; every detector's
validator here uses `10x` instead — see `ml-flag-detection/README.md` for
why (it coincided with host memory exhaustion during testing on this
project's original machine).

`productCatalogLockContention`, one of the 12 flags originally targeted
for this whole project, does not exist in this fork's `demo.flagd.json`
and is skipped everywhere.

## 4. Setting up and running each detector

All five detectors default to `http://localhost:9200` with credentials
read from `opentelemetry-demo/elastic-start-local/.env` — nothing to
configure for local use beyond having §2 running. (Setting `ES_URL`/
`ES_API_KEY` switches every one of these to a remote cluster instead — see
`RUNNING_ON_ELASTIC_CLOUD.md`.)

### 4a. `ml-flag-detection/` — supervised ML classifier

```bash
cd ml-flag-detection
pip install requests scikit-learn pandas numpy matplotlib joblib

# Collect a labeled dataset (~40-70 min; resumable with --append if interrupted)
python collect_dataset.py --out dataset.csv

# Train + evaluate
python train_classifier.py --data dataset.csv

# Score one live window against the trained model (no retraining)
python predict_and_bundle.py --model model.joblib --out result.json
```

Not a live/continuous detector — it's trained once, then predicts which
flag (if any) a fresh 25-40s telemetry window looks like. See its README
§6 for the actual numbers (55% test accuracy) and §7 for known gaps.

### 4b. `causal-changepoint-detection/` — statistical change-point + causal ranking

```bash
cd causal-changepoint-detection

# (Re)derive the service call graph -- worth re-running after enough uptime
# to exercise rare paths (checkout -> payment/email need real checkout traffic)
python derive_service_graph.py --minutes 240

# One-shot live scan
python changepoint_detector.py --lookback 15 --bucket 20 --out result.json

# Full 11-flag validation battery (~20-30 min -- each scan is 24 sequential ES|QL queries)
python validate_against_demo.py --settle 60 --lookback 15 --bucket 20
```

No pip installs beyond the standard library. `--verbose` on
`changepoint_detector.py` prints real ES|QL errors instead of silently
treating them as "no signal" — use it while developing; two real bugs
found while building this both looked identical to a quiet system until
`--verbose` was added.

### 4c. `probe-detector/` — z-score anomaly scanner (PROBE's "Detector" stage)

```bash
cd probe-detector

# One-shot live scan
python detector.py --out result.json

# Full 11-flag validation battery (~15-20 min)
python validate_against_demo.py --settle 15
```

No pip installs beyond the standard library. This is the fastest of the
four to react (5 lightweight aggregation queries per scan, ~10s polling
in the validator) and had the best detection rate in testing — see its
README §3.

### 4d. `elastic-ml-anomaly-detection/` — Elastic's own built-in ML anomaly detection

Needs the [`elastic` CLI](https://github.com/elastic/cli):

```bash
npm install -g @elastic/cli

# Point it at your local cluster (ES_LOCAL_PASSWORD is in the .env from step 2b)
elastic config context add local-otel-demo \
  --es-url "http://localhost:9200" --es-username elastic --es-password "$ES_LOCAL_PASSWORD" \
  --kb-url "http://localhost:5601" --kb-username elastic --kb-password "$ES_LOCAL_PASSWORD"
elastic config current-context set local-otel-demo
elastic status   # confirms connectivity -- do this before anything else
```

Then create the two jobs, in this exact order (job → datafeed → open job
→ start datafeed — the skill this was built with is explicit that this
order is mandatory):

```bash
cd elastic-ml-anomaly-detection
elastic es ml put-job --input-file jobs/job-latency.json
elastic es ml put-datafeed --input-file jobs/datafeed-latency.json
elastic es ml open-job --job-id otel-demo-latency
elastic es ml start-datafeed --datafeed-id datafeed-otel-demo-latency

elastic es ml put-job --input-file jobs/job-error-rate.json
elastic es ml put-datafeed --input-file jobs/datafeed-error-rate.json
elastic es ml open-job --job-id otel-demo-error-rate
elastic es ml start-datafeed --datafeed-id datafeed-otel-demo-error-rate

# Verify both are actually running, not just created
elastic es ml get-job-stats --job-id otel-demo-latency
elastic es ml get-datafeed-stats --datafeed-id datafeed-otel-demo-latency
```

**Important:** starting a datafeed with no explicit start time backfills
the job through the *entire* matching index history immediately, not just
from "now" forward — expect it to process everything in seconds, not
gradually in real time.

```bash
# Cross-reference against the known fault windows in ml-flag-detection/dataset.csv
python validate_against_history.py

# Live one-shot scan, same shared result schema as the other three detectors
python scan_current.py --minutes 30 --out result.json
```

**Read that folder's README §3 before trusting a `0/11` result at face
value** — it's a confirmed, real finding (the job's 5-minute bucket span
dilutes this repo's 25-40s fault windows), not evidence the method is
broken.

Teardown when done (stop datafeed before closing job, always in that
order):

```bash
elastic es ml stop-datafeed --datafeed-id datafeed-otel-demo-latency
elastic es ml close-job --job-id otel-demo-latency
elastic es ml stop-datafeed --datafeed-id datafeed-otel-demo-error-rate
elastic es ml close-job --job-id otel-demo-error-rate
```

### 4e. `probe-two-tier-detector/` — two-tier pipeline (`PROBE-detector-spec.md`)

```bash
cd probe-two-tier-detector

# One-shot scan: Tier 1 (z-score) shouts, Tier 2 (CHANGE_POINT) confirms every candidate
python detector.py --out result.json

# Full 11-flag validation battery (~20-30 min)
python validate_against_demo.py --settle 15

# Call Tier 2 on its own, the way a future Correlator would (e.g. one hop
# further up the dependency graph from a service Tier 1 never shouted about)
python change_point.py ad p95_latency --lookback 5 --bucket 2
```

No pip installs beyond the standard library. This is `probe-detector`'s
z-score scan (Tier 1) gated by a `CHANGE_POINT` significance test per
candidate (Tier 2) — no ranking, no root cause, exactly per the spec.
**Avoid retesting the same flag twice within its own 10-minute lookback
window** — the second activation's "baseline" ends up contaminated by the
first, collapsing the z-score (see its README §4 for exactly how this was
found).

### 4f. `probe-two-tier-detector-v2/` — Tier 1 fix (`FIX-tier1-zscore.md`)

```bash
cd probe-two-tier-detector-v2

# 9 unit tests, no ES needed — dilution/contended-baseline/persistence/error-floor/partial-bucket
python test_zscore.py

# One-shot scan (streak starts at 0 -- persistence needs repeated calls, see --loop)
python detector.py --out result.json

# Long-running scan, so persistence (2 consecutive shouts) can actually fire
python detector.py --loop 10

# Full battery per FIX-tier1-zscore.md §6: 5 cycles/flag + 3 null windows
python validate_against_demo.py
```

No pip installs beyond the standard library. Tier 1 rebuilt (median/MAD
z-score, no window dilution, persistence, error-count floor); Tier 2
(`change_point.py`) copied unchanged from v1. **Read its README §5 before
citing its numbers** — the battery there was intentionally stopped after
8 of 11 flags to free the demo stack for `probe-two-tier-detector-v3/`'s
own run; it's real partial data, not a completed battery.

### 4g. `probe-two-tier-detector-v3/` — Tier 2 fixes

```bash
cd probe-two-tier-detector-v3
python test_zscore.py           # same 9 Tier-1 tests, unchanged from v2
python test_change_point.py     # 3 new Tier-2 tests (insufficient-data, earliest-break, cascade batching)
python detector.py --out result.json
python validate_against_demo.py
```

Builds on v2 unchanged for Tier 1; fixes `change_point.py` and its caller
in `detector.py` — see its README for the full list of changes once its
battery completes.

## 5. Where the numbers come from

`DETECTORS.md` §3 has the full four-way comparison table (per-flag,
per-detector) — this doc is deliberately just the "how to run it" half.
