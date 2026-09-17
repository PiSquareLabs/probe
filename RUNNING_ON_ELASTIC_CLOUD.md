# Running against Elastic Cloud / Serverless

Everything in this repo defaults to the local self-hosted Elasticsearch
described in [`RUNNING_LOCALLY.md`](RUNNING_LOCALLY.md). This document is
the same demo app and same five detectors, pointed at a real Elastic
Cloud (or Serverless) deployment instead — no local Elasticsearch/Kibana
containers at all.

**Caveat up front, stated plainly:** every detector's own testing and the
numbers in `DETECTORS.md` were produced against the *local self-hosted*
setup. The demo app and each detector's Elasticsearch connection are both
genuinely configurable for Cloud (real environment variables, not
aspirational — see §3), but running the full 11-flag validation batteries
against Cloud specifically was not re-done for this document. Expect the
same detection behavior in principle (same index/mapping shape, same
queries) — but re-run the validators (§4) yourself before trusting a
Cloud-specific number.

## 1. Requirements

- An **Elastic Cloud** deployment or **Serverless Observability project**
  you can create API keys against. Free trial is sufficient for
  everything except `elastic-ml-anomaly-detection/` (§4d), which needs
  ML enabled — included on Serverless, needs at least a Platinum-tier
  equivalent on Cloud Hosted.
- Your deployment's:
  - **OTLP ingest endpoint** (Cloud: found under your deployment's
    "APM & Fleet" / integrations settings; Serverless: your project's
    OTLP endpoint).
  - **An API key** with permission to ingest data (for the demo app) and
    a separate one with `manage_ml`, read, and query privileges (for the
    detectors) — a single sufficiently-privileged key can serve both if
    you'd rather not manage two.
  - **Your Elasticsearch endpoint URL** (e.g.
    `https://my-deployment.es.us-east-1.aws.elastic.cloud`) — different
    from the OTLP ingest endpoint above.
- Docker Desktop (same requirements as `RUNNING_LOCALLY.md` §1, minus the
  Elasticsearch/Kibana containers — Cloud handles that side).
- Python 3.11+, same as local.

## 2. Bring up the demo application, pointed at Cloud

### 2a. Clone and pull the submodule

Same as local:

```bash
git clone <this-repo-url>
cd <this-repo>
git submodule update --init
```

### 2b. Configure the OTLP endpoint and API key

Edit `opentelemetry-demo/.env.override` (create it if it doesn't exist)
and set:

```bash
ELASTIC_OTLP_ENDPOINT="https://<your-deployment>.apm.<region>.<provider>.elastic.cloud:443"
ELASTIC_OTLP_API_KEY="<your ingest api key>"
```

Do **not** also copy in `demo-overlay/.env.override`'s contents here —
that file is for the *local self-hosted* port-remap/rename scenario and
its `ELASTIC_OTLP_ENDPOINT="YOUR_ENDPOINT"` placeholder would override
what you just set if applied afterward.

### 2c. Bring up the demo's microservices — no self-hosted overlay, no `elastic-start-local`

```bash
cd opentelemetry-demo
docker compose --env-file .env --env-file .env.override \
  -f compose.yaml -f compose.full.yaml \
  -f compose.observability.yaml -f compose.extras.yaml \
  -f docker-compose.elastic.yml \
  up -d
```

Note what's different from the local command in `RUNNING_LOCALLY.md` §2c:
**no** `docker-compose.elastic-self-hosted.yml` (that file exists
specifically to wire the collector to a local `elastic-start-local`
network, which doesn't exist here), and `compose.observability.yaml` +
`compose.extras.yaml` are included since there's no local
Elasticsearch/Kibana/EDOT stack of your own to skip them for.

The same two real bugs from `RUNNING_LOCALLY.md` §2c (stale prebuilt
images, CRLF line endings on `load-generator/entrypoint.sh`) apply
identically here — they're properties of the demo's Docker images, not of
which Elasticsearch backend you're pointed at.

### 2d. Confirm telemetry is arriving

In Kibana (or via `_query`/ES|QL directly against your Cloud
Elasticsearch endpoint):

```
FROM traces-*.otel-default | LIMIT 5
```

If this returns nothing after a few minutes, double-check the OTLP
endpoint/API key in `.env.override` and that `otel-collector`'s logs
(`docker logs otel-collector`) don't show export errors.

## 3. Pointing every detector at Cloud

Every detector script in this repo reads the same three environment
variables, checked in this order, falling back to the local self-hosted
default when none are set:

| Variable | Purpose |
|---|---|
| `ES_URL` | Your Elasticsearch endpoint (not the OTLP ingest endpoint — see §1). Defaults to `http://localhost:9200`. |
| `ES_API_KEY` | Preferred on Cloud/Serverless. Sent as `Authorization: ApiKey <value>`. |
| `ES_USERNAME` / `ES_PASSWORD` | Basic-auth alternative to `ES_API_KEY`. `ES_USERNAME` defaults to `elastic` if only `ES_PASSWORD` is set. |

Set them once per shell session before running any detector:

```bash
export ES_URL="https://my-deployment.es.us-east-1.aws.elastic.cloud"
export ES_API_KEY="<your query api key, base64-encoded id:api_key form>"
```

(PowerShell: `$env:ES_URL = "..."`, `$env:ES_API_KEY = "..."`.)

With these set, `opentelemetry-demo/elastic-start-local/.env` (which
holds the *local* Elasticsearch's generated password) is never read —
you don't need `start-local` installed at all for this path.

This applies to all seven detectors identically — `ml-flag-detection/`,
`causal-changepoint-detection/`, `probe-detector/`, `elastic-ml-anomaly-detection/`,
`probe-two-tier-detector/`, `probe-two-tier-detector-v2/`, and
`probe-two-tier-detector-v3/` all check the same three variables in their
respective `es_client`/query modules; see each file's module docstring for
the one-line note pointing back here.

## 4. Setting up and running each detector against Cloud

Once `ES_URL`/`ES_API_KEY` are exported, every command from
`RUNNING_LOCALLY.md` §4 works unchanged — the scripts themselves don't
need different invocations, only the environment variables above need to
be set first. Repeated here for completeness with the two things that do
differ:

### 4a. `ml-flag-detection/`

```bash
cd ml-flag-detection
pip install requests scikit-learn pandas numpy matplotlib joblib
python collect_dataset.py --out dataset.csv
python train_classifier.py --data dataset.csv
python predict_and_bundle.py --model model.joblib --out result.json
```

Identical to local once `ES_URL`/`ES_API_KEY` are exported.

### 4b. `causal-changepoint-detection/`

```bash
cd causal-changepoint-detection
python derive_service_graph.py --minutes 240
python changepoint_detector.py --lookback 15 --bucket 20 --out result.json
python validate_against_demo.py --settle 60 --lookback 15 --bucket 20
```

Identical to local. Cloud/Serverless ES|QL supports `CHANGE_POINT` the
same as self-hosted 9.5.3+; if you're on an older Cloud deployment that
predates it, this detector's queries will fail outright (not silently) —
check with `--verbose`.

### 4c. `probe-detector/`

```bash
cd probe-detector
python detector.py --out result.json
python validate_against_demo.py --settle 15
```

Identical to local.

### 4d. `elastic-ml-anomaly-detection/` — the one part that's genuinely different

The `elastic` CLI context needs Cloud credentials instead of local
basic-auth ones:

```bash
npm install -g @elastic/cli

elastic config context add cloud-otel-demo \
  --es-url "https://my-deployment.es.us-east-1.aws.elastic.cloud" \
  --es-api-key "<your api key>"
elastic config current-context set cloud-otel-demo
elastic status
```

From here, job/datafeed creation is identical to `RUNNING_LOCALLY.md`
§4d:

```bash
cd elastic-ml-anomaly-detection
elastic es ml put-job --input-file jobs/job-latency.json
elastic es ml put-datafeed --input-file jobs/datafeed-latency.json
elastic es ml open-job --job-id otel-demo-latency
elastic es ml start-datafeed --datafeed-id datafeed-otel-demo-latency
# ...repeat for jobs/job-error-rate.json / datafeed-error-rate.json
```

**License check first:** ML anomaly detection needs a Platinum-equivalent
license on Cloud Hosted (included by default on a trial, and always
included on Serverless). If `put-job` fails with a license error, that's
why — not a config mistake.

`scan_current.py` and `validate_against_history.py` both pick up
`ES_URL`/`ES_API_KEY` the same way as every other script here:

```bash
python scan_current.py --minutes 30 --out result.json
python validate_against_history.py
```

### 4e. `probe-two-tier-detector/`

```bash
cd probe-two-tier-detector
python detector.py --out result.json
python validate_against_demo.py --settle 15
```

Identical to local once `ES_URL`/`ES_API_KEY` are exported — both tiers
(`zscore_scan.py`'s `es_client.py` and `change_point.py`'s own auth
helper) check the same three variables. `CHANGE_POINT`'s Cloud/Serverless
availability note from §4b applies here too, since Tier 2 is built on the
same ES|QL command.

### 4f. `probe-two-tier-detector-v2/` and `probe-two-tier-detector-v3/`

```bash
cd probe-two-tier-detector-v2   # or probe-two-tier-detector-v3
python detector.py --out result.json
python validate_against_demo.py
```

Identical to local once `ES_URL`/`ES_API_KEY` are exported — same
`es_client.py`/auth pattern as every other detector here. See
`RUNNING_LOCALLY.md` §4f-4g for what each version actually changed.

## 5. Switching back to local

Just unset the environment variables (or open a new shell that never set
them) — every script falls back to `http://localhost:9200` and the local
`elastic-start-local/.env` password automatically:

```bash
unset ES_URL ES_API_KEY ES_USERNAME ES_PASSWORD
```
