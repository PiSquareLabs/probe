# PROBE — Day-0 Cluster Verification Checklist
### Ranked by demo risk. Work top-down. Do NOT skip to the interesting ones.

Everything below is **[UNTESTED]** by definition — I have no cluster. Each item
gives the exact check and what to do when it fails.

---

## 🔴 BLOCKERS — if any of these fail, the demo does not exist

### D0-1 · Platinum/trial licence (CHANGE_POINT is licence-gated)
```
GET /_license
POST /_license/start_trial?acknowledge=true
```
**[DOCUMENTED F1]** "The `CHANGE_POINT` command requires a platinum license."
On Basic it **errors**. A 30-day trial gives platinum.
**Do this first, days early** — a trial started on stage is a lost demo.
❌ If unavailable: PROBE has no detector. Fallback = ML anomaly-detection job
(also platinum) or a hand-rolled z-score in ES|QL. **Both are worse. Solve this.**

### D0-2 · Stack ≥ 9.5 (for `CHANGE_POINT ... BY`)
```
GET /
```
**[DOCUMENTED F1]** `BY` is GA only in **9.5+**. Below that there is **no** way
to detect change points for all services in one query.
❌ Fallback: loop `01-detect...esql` once per service from the agent (~15 calls).
Works, slower, still demoable.

### D0-3 · Field names actually resolve
```esql
FROM traces-*.otel-* | LIMIT 1
FROM traces-*.otel-* | KEEP @timestamp, trace_id, span_id, parent_span_id,
     kind, duration, status.code, resource.attributes.service.name | LIMIT 5
```
Then confirm the **short passthrough form** works — or that it doesn't:
```esql
FROM traces-*.otel-* | STATS n = COUNT(*) BY service.name | LIMIT 5
```
**[DOCUMENTED F3]** `resource.attributes` is a `passthrough` field, which should
expose `service.name` at the root. **[UNTESTED]** that ES|QL resolves it.
✅ **Use the full path `resource.attributes.service.name` everywhere regardless.**

### D0-4 · `kind` literal — the silent zero-rows trap
```esql
FROM traces-*.otel-* | STATS n = COUNT(*) BY kind
```
**[DOCUMENTED F4]** Expect **`Server` / `Client` / `Internal`**.
If you see `SPAN_KIND_SERVER`, the collector is in a non-OTel mapping mode —
**every query in this repo must be rewritten**, and `duration` will be
**microseconds**, not nanoseconds.
❌ `WHERE kind == "SPAN_KIND_SERVER"` returns **zero rows with no error**.
This is the single most likely way to be embarrassed on stage.

### D0-5 · `LOOKUP JOIN` after `STATS`/`CHANGE_POINT` (single cluster)
```esql
FROM traces-*.otel-*
| WHERE @timestamp >= NOW() - 20 minutes AND kind == "Server"
| EVAL svc = resource.attributes.service.name
| STATS calls = COUNT(*) BY ts = BUCKET(@timestamp, 10 seconds), svc
| CHANGE_POINT calls ON ts BY svc
| WHERE type IS NOT NULL
| EVAL caller = svc
| LOOKUP JOIN probe-service-graph ON caller
| LIMIT 10
```
**[DOCUMENTED F5]** The "not after pipeline-breaking commands" restriction is
scoped to **cross-cluster**. **[REASONING]** single-cluster should be fine.
**This one query proves or kills the whole causal design. Run it first.**
❌ Fallback: write Stage 1 output to `probe-anomaly-current`, run Stage 2 as a
separate query starting `FROM probe-anomaly-current`. Costs one round trip.
*(The ES|QL in `03-causal-rank.esql` is already written in this safer shape.)*

### D0-6 · Bedrock inference endpoint returns a FULL answer
```
PUT /_inference/completion/probe-bedrock-claude { ... }
POST /_inference/completion/probe-bedrock-claude
{ "input": "List the nine planets, one per line, with one sentence each." }
```
**[DOCUMENTED F7]** `max_new_tokens` **defaults to 64** → the verdict truncates
mid-sentence. Set ≥1024. Also: `temperature` and `top_p`/`top_k` are mutually
exclusive — send only one.
**[DOCUMENTED F10]** In **ap-south-1** Claude is served via **cross-Region
inference profiles**; `model` must be a profile ID (e.g. `global.anthropic.…`),
not a bare model ID, or Bedrock rejects it with *"on-demand throughput isn't
supported."* **Get the exact profile ID from the Bedrock console that morning.**
Also note keys are **write-once** — rotating means delete + recreate.

---

## 🟠 HIGH — breaks a headline moment

### D0-7 · ≥22 buckets per series exist
```esql
FROM traces-*.otel-* | WHERE @timestamp >= NOW() - 20 minutes AND kind == "Server"
| EVAL svc = resource.attributes.service.name
| STATS n = COUNT(*) BY ts = BUCKET(@timestamp, 10 seconds), svc
| STATS buckets = COUNT(*) BY svc | SORT buckets ASC
```
**[DOCUMENTED F1]** <22 ⇒ that service is **silently skipped**. Low-traffic
services (`email`, `accounting`) are the risk. Fix by widening the window or
raising the load generator rate.

### D0-8 · The service graph is populated and correct
```esql
FROM probe-service-graph | STATS edges = COUNT(*) BY caller | SORT edges DESC
```
Expect `frontend` and `checkout` as high-degree hubs. If `probe-service-graph`
is empty, **Stage 2 returns nothing and looks like a bug on stage.**
Populate via the collector `service_graph` connector (**[DOCUMENTED]**, emits
`traces_service_graph_request_total{client,server}`, **stability: alpha**,
renamed from `servicegraph`) **or** pre-materialise it. **[UNTESTED]** whether
the **EDOT** collector bundles that connector — if not, run a sidecar
`otelcol-contrib`, or pre-materialise. **Pre-materialising is the safe demo choice.**

### D0-9 · `CHANGE_POINT` inside a `FORK` branch
Run `05-baseline-vs-probe-fork.esql`.
**[UNTESTED]** — undocumented combination. ❌ Fallback `05b` is already written.

### D0-10 · `VALUES()` availability
**[DOCUMENTED]** `VALUES` is marked **preview**. Used in `03` and `04`.
❌ Fallback: `TOP(svc, 20, "asc")`.

---

## 🟡 MEDIUM

### D0-11 · Real `service.name` values
```esql
FROM traces-*.otel-* | STATS n = COUNT(*) BY resource.attributes.service.name
```
Confirm hyphenation: `product-catalog`, `frontend-proxy`, `image-provider`
(per `.env`). `probe-ground-truth` must use the **exact** strings or grading
scores 0% while looking correct.

### D0-12 · `::` cast and `DATE_DIFF` behave
```esql
ROW a = 3, b = 4 | EVAL r = a::double / b::double
ROW t1 = NOW() | EVAL d = DATE_DIFF("seconds", t1, NOW())
```

### D0-13 · ELSER deployed
```
GET /_inference/sparse_embedding/probe-elser
```
ELSER needs an ML node and a download. **Do this days early, not on the day.**

### D0-14 · `esql.command.completion.limit`
**[DOCUMENTED F6]** 9.3+ caps `COMPLETION` at **100 rows** by default. PROBE
sends 1, so this is fine — but know the number if asked.

### D0-15 · Agent Builder present
```
GET /api/agent_builder/tools
```
Needs Kibana **9.2+**. Workflows need **9.3+** (tech preview).
