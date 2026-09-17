# Deployment guide: demo app + all seven detectors, local or Elastic Cloud

One linear walkthrough combining `RUNNING_LOCALLY.md` and
`RUNNING_ON_ELASTIC_CLOUD.md` into a single step-by-step path. Use this
document to go from a clean checkout to all seven detectors running
against real telemetry. `DETECTORS.md` remains the narrative/comparison
reference; this document is the "do this, then this" version.

## Part A — Deploy the demo app

### A1. Prerequisites

- Docker Desktop, ≥8GB RAM budgeted to it specifically.
- Python 3.11+.
- ~10GB free disk.
- (Cloud path only) an Elastic Cloud deployment or Serverless
  Observability project, its OTLP endpoint, and an API key.

### A2. Clone and pull the submodule

```bash
git clone <this-repo-url>
cd <this-repo>
git submodule update --init          # pulls opentelemetry-demo/
```

### A3. Choose local self-hosted OR Elastic Cloud, then bring up telemetry backend

**Local (self-hosted Elasticsearch + Kibana):**
```bash
cd opentelemetry-demo
curl -fsSL https://elastic.co/start-local | sh -s -- --edot
```
This creates `opentelemetry-demo/elastic-start-local/.env` with generated
credentials — every detector's `es_client.py` reads
`ES_LOCAL_PASSWORD` from this file automatically when no `ES_*`
environment variables are set. No further action needed for this path.

**Elastic Cloud / Serverless (skip the step above):** edit
`opentelemetry-demo/.env.override` (create if missing):
```bash
ELASTIC_OTLP_ENDPOINT="https://<your-deployment>.apm.<region>.<provider>.elastic.cloud:443"
ELASTIC_OTLP_API_KEY="<your ingest api key>"
```

### A4. Bring up the demo microservices

**Local:**
```bash
cd opentelemetry-demo
docker compose --env-file .env \
  -f compose.yaml -f compose.full.yaml \
  -f docker-compose.elastic.yml -f docker-compose.elastic-self-hosted.yml \
  up -d
```

**Cloud:** same command, but drop `docker-compose.elastic-self-hosted.yml`
and add `compose.observability.yaml -f compose.extras.yaml` (no local
Elasticsearch/Kibana of your own to skip them for):
```bash
cd opentelemetry-demo
docker compose --env-file .env --env-file .env.override \
  -f compose.yaml -f compose.full.yaml \
  -f compose.observability.yaml -f compose.extras.yaml \
  -f docker-compose.elastic.yml \
  up -d
```

`compose.full.yaml` is required either way — it defines
`kafka`/`accounting`/`fraud-detection`.

**Two known first-bringup bugs, either path:**
1. Stale prebuilt images: `docker compose build frontend-proxy
   load-generator checkout shipping` if you see envoy config errors or
   missing binaries on healthcheck.
2. CRLF line endings on `src/load-generator/entrypoint.sh` (Windows
   checkout) breaking its shebang: `sed -i 's/\r$//'
   src/load-generator/entrypoint.sh`, then rebuild that image.

### A5. Confirm telemetry is arriving

```
FROM traces-*.otel-default | LIMIT 5
```
Run this via Kibana Dev Tools or a direct `_query` call. Nothing after a
few minutes → check `docker logs otel-collector` for export errors, and
double check the OTLP endpoint/API key (Cloud path) or that
`elastic-start-local` containers are up (local path).

### A6. Fault injection mechanism (used to test every detector below)

Edit `opentelemetry-demo/src/flagd/demo.flagd.json`, set one flag's
`defaultVariant` to its "on" value; flagd hot-reloads with no restart.
Set it back to `"off"` afterward. Full flag table (11 flags, target
service, "on" variant) is in `DETECTORS.md` §1d.

## Part B — Point every detector at the right cluster

Every detector script reads the same three environment variables,
checked in this order, falling back to the local `elastic-start-local`
password when none are set:

| Variable | Purpose |
|---|---|
| `ES_URL` | Elasticsearch endpoint. Default `http://localhost:9200`. |
| `ES_API_KEY` | Preferred for Cloud/Serverless (`Authorization: ApiKey ...`). |
| `ES_USERNAME` / `ES_PASSWORD` | Basic-auth alternative. |

**Local path:** set nothing — every detector falls back automatically.

**Cloud path:** export once per shell before running any detector below:
```bash
export ES_URL="https://my-deployment.es.us-east-1.aws.elastic.cloud"
export ES_API_KEY="<query api key, base64 id:api_key form>"
```
(PowerShell: `$env:ES_URL = "..."`, `$env:ES_API_KEY = "..."`.)

To switch back to local: `unset ES_URL ES_API_KEY ES_USERNAME ES_PASSWORD`.

## Part C — Deploy each detector

No pip installs beyond the standard library are needed for any detector
except `ml-flag-detection/` (scikit-learn/pandas/etc.) and
`elastic-ml-anomaly-detection/` (the `elastic` CLI, npm).

### C1. `ml-flag-detection/` — supervised classifier

```bash
cd ml-flag-detection
pip install requests scikit-learn pandas numpy matplotlib joblib
python collect_dataset.py --out dataset.csv     # ~40-70 min; injects all 11 flags to label training data
python train_classifier.py --data dataset.csv   # seconds
python predict_and_bundle.py --model model.joblib --out result.json
```

### C2. `causal-changepoint-detection/` — change-point + causal ranking

```bash
cd causal-changepoint-detection
python derive_service_graph.py --minutes 240
python changepoint_detector.py --lookback 15 --bucket 20 --out result.json
python validate_against_demo.py --settle 60 --lookback 15 --bucket 20
```

### C3. `probe-detector/` — z-score anomaly scanner

```bash
cd probe-detector
python detector.py --out result.json
python validate_against_demo.py --settle 15
```

### C4. `elastic-ml-anomaly-detection/` — Elastic's built-in ML jobs

```bash
npm install -g @elastic/cli
elastic config context add otel-demo --es-url "$ES_URL" --es-api-key "$ES_API_KEY"
elastic config current-context set otel-demo
elastic status

cd elastic-ml-anomaly-detection
elastic es ml put-job --input-file jobs/job-latency.json
elastic es ml put-datafeed --input-file jobs/datafeed-latency.json
elastic es ml open-job --job-id otel-demo-latency
elastic es ml start-datafeed --datafeed-id datafeed-otel-demo-latency
# repeat for jobs/job-error-rate.json / datafeed-error-rate.json

python scan_current.py --minutes 30 --out result.json
python validate_against_history.py
```
Needs a Platinum-equivalent license (included on trial/Serverless) —
`put-job` fails with a license error otherwise, not a config mistake.

### C5. `probe-two-tier-detector/` — two-tier pipeline (v1)

```bash
cd probe-two-tier-detector
python detector.py --out result.json
python validate_against_demo.py --settle 15
python change_point.py ad p95_latency   # Tier 2 called standalone
```

### C6. `probe-two-tier-detector-v2/` — Tier 1 fix

```bash
cd probe-two-tier-detector-v2
python test_zscore.py                # 9 unit tests, no ES needed
python detector.py --loop 10          # long-running; persistence needs repeated scans
python validate_against_demo.py       # 5 cycles/flag + 3 null windows
```

### C7. `probe-two-tier-detector-v3/` — Tier 2 fix

```bash
cd probe-two-tier-detector-v3
python test_zscore.py           # same Tier-1 tests, unchanged from v2
python test_change_point.py     # Tier-2 tests: insufficient-data, earliest-break, cascade batching
python detector.py --out result.json
python validate_against_demo.py
```

## Part D — Adding a detector's results as a document

Each detector's `validate_against_demo.py` writes `validation_results.json`
in its own folder when it finishes. To turn a run into a shareable
write-up:

1. Run the detector's validator (Part C above) to produce
   `validation_results.json` in that folder.
2. Add or update that detector's own `README.md` with a results section:
   what was run, the per-flag table, and an honest read of the misses
   (every README in this repo already follows this pattern — copy the
   structure from `probe-two-tier-detector-v2/README.md` §5 as a
   template).
3. Update `DETECTORS.md`'s comparison table (§3) with a new column/row
   for that detector's numbers, and its "master index" table (top of the
   file) with a one-line method description.
4. If it's a new detector folder (not an update to an existing one), add
   a `### <name>/` subsection to this guide's Part C, and to
   `RUNNING_LOCALLY.md` §4 / `RUNNING_ON_ELASTIC_CLOUD.md` §4, so all
   three run-instruction documents stay in sync.

No detector writes directly to a shared "results" store — each one's
`validation_results.json` and README are the artifact; DETECTORS.md is
just the index tying them together.
