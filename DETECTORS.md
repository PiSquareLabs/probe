# Detectors: master index

This repo's `backend/`+`frontend/` is the **Remediator** stage of PROBE
(see `docs/RESEARCH.md`/`docs/ARCHITECTURE.md`): it files the Jira ticket
once a root cause is known. This document covers the other side of that
pipeline — the **Detector** stage, which finds the anomaly in the first
place. Four independent detection methods were built and validated
against the Elastic OpenTelemetry demo stack (the "Astronomy Shop",
`opentelemetry-demo/` — a git submodule). Each lives in its own top-level
folder with its own detailed README; this document is the map between
them, the shared demo setup they all depend on, and a single comparison
table.

No Correlator stage (the LLM-via-Bedrock reasoning step that would pick
one cause from a detector's candidate list) exists in this repo yet —
`causal-changepoint-detection/`'s heuristic ranking is this project's
current best stand-in for it, and its README is candid about where that
heuristic falls short (see its §5a).

| Folder | Method | Needs training data? | Names a single root cause? |
|---|---|---|---|
| `ml-flag-detection/` | Supervised ML (`RandomForestClassifier`) on aggregated telemetry windows | Yes (65 labeled windows) | Implicitly (predicts the class = the flag) |
| `causal-changepoint-detection/` | Unsupervised statistical change-point detection (ES|QL `CHANGE_POINT`) + call-graph ranking | No | Yes — explicit Layer 4 ranking |
| `probe-detector/` | Unsupervised z-score anomaly scoring over bucketed aggregations (implements the "Detector" stage from `idea/probe.pdf`'s 3-agent PROBE design) | No | No — deliberately over-inclusive; a Correlator stage would rank these |
| `elastic-ml-anomaly-detection/` | Elastic's own built-in ML anomaly detection jobs (`high_mean`/`high_count`, partitioned by service), set up via the `elasticsearch-anomaly-detection` Claude skill | No (learns online) | No |

`elastic-ml-anomaly-detection/` is the odd one out: its default 5-minute
bucket span turned out to be a poor match for this repo's 25-40s
fault-injection windows (0/11 flags detected — see its README §3 for the
full, honest analysis of why, confirmed at both the record and bucket
level, not just a threshold-tuning issue).

`.claude/skills/` in this repo has all 26 skills from
[`elastic/agent-skills`](https://github.com/elastic/agent-skills)
installed, including `elasticsearch-anomaly-detection` (used to build the
fourth detector above) and `elasticsearch-anomaly-detection-explainer`
(the natural next step for interpreting a job's results once it's running
against real, sustained traffic rather than this repo's short synthetic
faults).

All four query the same Elasticsearch instance, use the same flagd faults
for validation, and independently rediscovered the same "flagd's own
OpenFeature client pollutes error-rate/latency signals" bug — see each
folder's README for the specific fix, and `probe-detector/README.md` for
the cross-project note tying all three together (and the flagd-noise fix that a fourth, `elastic-ml-anomaly-detection/`, also had to apply to its own datafeed queries).

## 1. Standing up the demo (required for all four detectors)

### 1a. Requirements

- Docker Desktop, with the demo's containers given genuine headroom —
  budget at least 8GB of RAM for Docker specifically, more if anything
  else is running on the same host. On a resource-constrained Windows
  host, an over-allocated Docker Desktop/WSL2 VM can starve Windows itself
  and cause out-of-memory kills during long-running scripts; if that
  happens, check `C:\Users\<user>\.wslconfig`'s `memory=` setting first
  (this was root-caused in exactly that form while building this — see
  `ml-flag-detection/README.md` for the full story). Applying a
  `.wslconfig` change requires `wsl --shutdown` + restarting Docker
  Desktop, which restarts *every* container on the host, not just this
  demo — disruptive on a shared machine, plan accordingly.
- Python 3.11+. No virtualenv was used originally; packages were installed
  globally per-detector (see each folder's own requirements below) — using
  one is recommended for a clean checkout.
- ~10GB free disk for the demo images + Elasticsearch data.
- `git submodule update --init` after cloning this repo, to pull
  `opentelemetry-demo/`.
- **Only if** another OTel-demo instance is already running on the same
  Docker host: the container-renaming and port overlay in `demo-overlay/`
  (see that folder's README) is needed to avoid collisions. Skip it
  entirely on a clean host.

### 1b. Bring up Elasticsearch/Kibana/EDOT (self-hosted, no cloud account)

```bash
git submodule update --init          # first time only
cd opentelemetry-demo
curl -fsSL https://elastic.co/start-local | sh -s -- --edot
```

This creates `opentelemetry-demo/elastic-start-local/` — Elasticsearch,
Kibana, and an EDOT collector, plus a `.env` file with generated
credentials (`ES_LOCAL_PASSWORD` is what every detector's `es_client`
reads to authenticate). If Docker Desktop restarts, these containers do
**not** auto-restart (no restart policy set) — bring them back manually:
```bash
cd opentelemetry-demo/elastic-start-local
docker compose --env-file .env -f docker-compose.yml up -d
```

### 1c. Bring up the demo services

Plain bring-up (clean host, default ports):
```bash
cd opentelemetry-demo
docker compose --env-file .env \
  -f compose.yaml -f compose.full.yaml \
  -f docker-compose.elastic.yml -f docker-compose.elastic-self-hosted.yml \
  up -d
```

Only if you need the port/container-renaming overlay from `demo-overlay/`
(see §1a's last bullet):
```bash
cd opentelemetry-demo
cp ../demo-overlay/.env.override ../demo-overlay/docker-compose.rename.yml .
docker compose --env-file .env --env-file .env.override \
  -f compose.yaml -f compose.full.yaml \
  -f docker-compose.elastic.yml -f docker-compose.elastic-self-hosted.yml \
  -f docker-compose.rename.yml \
  up -d
```

- `compose.full.yaml` is **not optional** even though it looks like an
  add-on — it's what actually defines `kafka`/`accounting`/`fraud-detection`.
- `docker-compose.rename.yml` renames every container + the default
  network to avoid colliding with another instance of this same demo on
  the same host.
- `.env.override` remaps `ENVOY_PORT`/`ENVOY_ADMIN_PORT` off 8080/10000 and
  re-derives `K6_TARGET_URL`/`FRONTEND_PROXY_ADDR` to match — `.env`
  resolves those from the *old* port before `.env.override` is merged in,
  so overriding `ENVOY_PORT` alone silently leaves the load-generator
  pointed at the wrong port. Edit the port numbers in `demo-overlay/.env.override`
  if 18080/18090 also happen to be taken on your host.

**Two real bugs to expect on a from-scratch bring-up**, both documented in
depth in `ml-flag-detection/README.md`:
1. If any service's image was already built under the same
   `IMAGE_NAME:DEMO_VERSION` tag from a different commit, Docker won't
   rebuild it automatically — `frontend-proxy`, `load-generator`, `checkout`,
   and `shipping` all needed `docker compose build <service>` explicitly
   here. Symptoms: envoy config validation errors, `wget: not found`,
   `exec: no such file or directory` on healthcheck binaries.
2. `src/load-generator/entrypoint.sh` can get checked out with CRLF line
   endings on Windows, which breaks its `#!/bin/sh` shebang inside the
   Linux container. Fix: `sed -i 's/\r$//' src/load-generator/entrypoint.sh`
   then rebuild.

### 1d. flagd fault injection (how every detector below gets tested)

All four detectors are validated the same way: edit
`opentelemetry-demo/src/flagd/demo.flagd.json`, change one flag's
`defaultVariant` to its "on" value, wait, observe, then set it back to
`"off"`. flagd watches this file and hot-reloads it — no restart needed.
The 11 flags used (of 12 originally requested —
`productCatalogLockContention` does not exist in this fork) and the
variant used for "on":

| Flag | "on" variant | Target service |
|---|---|---|
| `adFailure` | `on` | `ad` |
| `adHighCpu` | `on` | `ad` |
| `adManualGc` | `on` | `ad` |
| `cartFailure` | `100%` | `cart` |
| `paymentFailure` | `100%` | `payment` |
| `recommendationCacheFailure` | `on` | `recommendation` |
| `imageSlowLoad` | `10sec` | `frontend` (see caveats — `image-provider` may be a better target) |
| `intlShippingSlowdown` | `10sec` | `shipping` |
| `productCatalogFailure` | `on` | `product-catalog` |
| `emailMemoryLeak` | `10x`\* | `email` |
| `kafkaQueueProblems` | `on` | *(no direct trace/metric target — Kafka isn't traced)* |

\* `emailMemoryLeak`'s strongest variant is `10000x`; dialed back after it
coincided with host memory exhaustion during dataset collection (see
`ml-flag-detection/README.md`).

## 2. Running each detector

### `ml-flag-detection/` — supervised classifier

```bash
cd ml-flag-detection
pip install requests scikit-learn pandas numpy matplotlib joblib
python collect_dataset.py --out dataset.csv     # ~40-70 min, resumable via --append
python train_classifier.py --data dataset.csv   # seconds
```
Not a live detector — it's trained once on a labeled dataset (this one:
10 baseline + 5 cycles x 11 flags = 65 windows) and then predicts which
flag was active from a held-out telemetry window. There is no "time to
detect" in the online sense; the unit of work is a 25-40s pre-recorded
window, and the model itself scores in milliseconds.

### `causal-changepoint-detection/` — statistical change-point + causal ranking

```bash
cd causal-changepoint-detection
python derive_service_graph.py --minutes 240   # rebuild the call graph if the fork/traffic changed
python changepoint_detector.py --lookback 15 --bucket 20   # one-shot live scan
python validate_against_demo.py --settle 60 --lookback 15 --bucket 20   # full 11-flag battery
```
No pip installs beyond the standard library. Each scan runs `CHANGE_POINT`
twice (latency + error rate) for each of 12 services — 24 sequential
ES|QL queries — so a single scan takes tens of seconds; budget accordingly
for continuous polling.

### `probe-detector/` — z-score anomaly scanner (PROBE's "Detector" stage)

```bash
cd probe-detector
python detector.py                                    # one-shot live scan
python validate_against_demo.py --settle 15            # full 11-flag battery, ~15-20 min
```
No pip installs beyond the standard library. A single scan is 5 lightweight
aggregation queries, so this comfortably supports the 10s polling interval
`validate_against_demo.py` uses.

### `elastic-ml-anomaly-detection/` — Elastic's own ML anomaly detection jobs

Requires the [`elastic` CLI](https://github.com/elastic/cli)
(`npm install -g @elastic/cli`) and a context pointed at your cluster
(`elastic config context add ...`, `elastic config current-context set ...`
— see that folder's README §1). The two jobs (`otel-demo-latency`,
`otel-demo-error-rate`) are already defined in `jobs/*.json`:

```bash
cd elastic-ml-anomaly-detection
elastic es ml put-job --input-file jobs/job-latency.json
elastic es ml put-datafeed --input-file jobs/datafeed-latency.json
elastic es ml open-job --job-id otel-demo-latency
elastic es ml start-datafeed --datafeed-id datafeed-otel-demo-latency
# repeat for jobs/job-error-rate.json / datafeed-error-rate.json

python validate_against_history.py   # cross-references ../ml-flag-detection/dataset.csv's known windows
python scan_current.py --out result.json   # live one-shot scan, same shared schema as the other three
```

**Read that folder's README §3 before trusting a 0/11 result at face
value** — it's a real, confirmed finding (bucket-span/fault-duration
mismatch), not evidence the method doesn't work in general.

## 3. Comparison table

Same 11 flags, same demo stack, same host — but **not** an apples-to-apples
methodology in every column (noted where it matters). "Detected" means the
fault's own target service appeared *somewhere* in the method's output;
"Correctly named as THE cause" only applies to the two methods that
attempt that (changepoint's ranking, and the classifier's prediction).

| Flag | Target | ML classifier (`ml-flag-detection`) | Changepoint detector (`causal-changepoint-detection`) | PROBE Detector (`probe-detector`) | Elastic ML (`elastic-ml-anomaly-detection`) |
|---|---|---|---|---|---|
| `adFailure` | `ad` | test-set miss (0 precision — no error-rate signal observed) | missed (`cart`/`currency` flagged instead) | **detected, 26.1s** | missed |
| `adHighCpu` | `ad` | **correct (2/2 test)** | detected, but ranked #2 behind `cart` | **detected, 26.5s** | missed |
| `adManualGc` | `ad` | **correct (1/1 test)** | detected, but ranked #2 behind `cart` (named correctly in an earlier isolated spot-check — see §3a) | **detected, 25.7s** | missed (score 0.0 — see its README §3) |
| `cartFailure` | `cart` | correct via a correlated feature, not error rate (see caveat in its README) | **named root cause** | missed (flagged `payment`/`quote` instead) | missed |
| `paymentFailure` | `payment` | test-set miss | missed (ranked `recommendation` #1) | **detected, 25.9s** | missed |
| `recommendationCacheFailure` | `recommendation` | **correct (1/1 test)** | missed (ranked `product-catalog` #1) | **detected, 87.2s** | missed |
| `imageSlowLoad` | `frontend` | **correct (1/1 test)** | detected, but ranked #2+ behind `product-catalog` | missed (flagged `recommendation` instead) | missed |
| `intlShippingSlowdown` | `shipping` | not enough traffic to `checkout`/`shipping` for a signal | missed entirely (`shipping` never a candidate) | missed | missed |
| `productCatalogFailure` | `product-catalog` | test-set miss | detected, but ranked #2+ behind `ad` | missed | missed |
| `emailMemoryLeak` | `email` | not observable — `email` emits no OTel memory metric in this fork | missed entirely (same reason — no metric to change on) | missed (same reason) | missed |
| `kafkaQueueProblems` | *(none)* | not directly attributable | not applicable — no signal in this detector's scope | not applicable — no signal in this detector's scope | not applicable — no signal in this detector's scope |
| **Overall** | | **55% test accuracy** (11/20 stratified test rows, 12 classes) | **5/11 detected; only 1/11 correctly named as #1 root cause** (see §3a — this got notably worse than an earlier single-flag spot-check, for reasons worth reading) | **5/11 detected, mean 38.3s** | **0/11 detected** — its 5-minute bucket span dilutes these 25-40s faults below any threshold; see its README §3 for why this is a granularity mismatch, not a broken job |

### 3a. Changepoint detector's full-battery result: a real regression worth understanding

An earlier, isolated single-flag test (`adManualGc` only) had `CHANGE_POINT`
catch the fault in seconds with p<1e-24 and the ranking layer name `ad`
correctly. The full 11-flag battery above, run later in the session, told
a substantially worse story: **`cart` (or `ad`, or `product-catalog`) wins
the #1 ranking in most rows almost regardless of which flag was actually
injected**, and `ad`/`cart`/`currency`/`product-catalog`/`recommendation`
recur as "anomalous" across nearly every unrelated flag. That pattern is
not consistent with 11 independent faults each having a distinct effect —
it's the signature of a shared confound. Two likely, non-exclusive causes,
detailed in `causal-changepoint-detection/README.md` §5a:

1. **Ambient host contention rose over the session** — running two full
   demo stacks + Elasticsearch/Kibana on a 15GB host (see §1a above) means
   the noise floor isn't static; hours in, it was visibly worse than at
   the start.
2. **This validator's own query load is much heavier than `probe-detector`'s**
   (24 sequential `CHANGE_POINT` queries per scan vs. 5 lightweight
   aggregations) and — unlike `probe-detector`'s adaptive polling — only
   takes one fixed-cost scan per flag (~110s: 60s settle + ~50s scan), so
   it has no chance to "get lucky" on a quieter moment the way a
   several-attempts-over-two-minutes method does.

This is a genuinely useful negative result, not just a worse number: it
shows the `CHANGE_POINT` *detection* layer still finds real candidates
(5/11), but the causal-*ranking* heuristic (earliness + call-graph
reachability) needs either a cleaner host to validate against, or a
ranking signal that can discount a candidate whose shift correlates with
host-level metrics rather than its own request volume — a concrete next
step rather than a vague "needs more work."
