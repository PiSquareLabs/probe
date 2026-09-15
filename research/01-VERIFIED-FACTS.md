# PROBE — Verified Facts Ledger

Every entry cites a **primary source**. Honesty tags:
- **[DOCUMENTED]** — stated in primary docs/source, URL given.
- **[REASONING]** — my inference from documented facts.
- **[UNTESTED]** — requires the live cluster to confirm.

> **Access note:** `www.elastic.co` and `docs.aws.amazon.com` are blocked by this
> session's egress policy. All Elastic facts below are therefore taken from
> **Elastic's own doc *source* repositories** on `raw.githubusercontent.com`
> (`elastic/elasticsearch`, `elastic/elasticsearch-specification`,
> `elastic/docs-content`), which is what generates the website — equal or higher
> authority. OTel facts come from `open-telemetry/*` source.

---

## F1. `CHANGE_POINT` is a COMMAND, not a function — and it is licence-gated

**[DOCUMENTED]** Source: `elastic/elasticsearch@main`
`docs/reference/query-languages/esql/_snippets/commands/layout/change_point.md`

```
serverless: ga
stack: preview =9.1, ga 9.2+
```

> **The `CHANGE_POINT` command requires a [platinum license].**

Syntax by version:

| Stack version | Syntax |
|---|---|
| 9.2 – 9.4 | `CHANGE_POINT value [ON key] [AS type_name, pvalue_name]` |
| **9.5+**  | `CHANGE_POINT value [ON key] [AS type_name, pvalue_name] [BY grouping_expr, ...]` |
| 9.6+ | adds detection of *multiple* change points per series |

Parameters (verbatim):
- `value` — the column with the metric in which to detect a change point.
- `key` — column to order values by. **If not specified, `@timestamp` is used.**
- `group` (9.5+) — "change point detection is performed independently for each group".
- `type_name` — output column for the change point type. Default column name `type`.
- `pvalue_name` — output column for the p-value. Default column name `pvalue`.

Output change-point types (exhaustive, verbatim): `dip`, `distribution_change`,
`spike`, `step_change`, `trend_change`.

> "There must be at least **22 values** for change point detection. Any values
> beyond the **first 1,000 are ignored**. When a `BY` clause is provided, these
> rules apply per group."

**p-value semantics (verbatim):** "the p-value that indicates how extreme the
change point is (**lower values indicate greater changes**)."

### Consequences for PROBE (these are load-bearing)
1. **[DOCUMENTED] Platinum licence required.** A Basic cluster returns an error.
   A **30-day trial** activates platinum features. → **Day-0 blocker item D0-1.**
2. **[DOCUMENTED] `BY` requires stack ≥ 9.5.** Repo `main` is `9.6.0` (see F2), so
   released GA is ~9.5.x. Detecting change points for *all services in one query*
   depends entirely on `BY`. On ≤9.4 you must issue one query per service.
   → **Day-0 blocker item D0-2.**
3. **[DOCUMENTED] ≥22 buckets, ≤1000 used.** Bucket width must be chosen so the
   demo window yields 22–1000 buckets. 10 min @ 5 s = 120 buckets. Safe.
   A 30 s bucket over 10 min = 20 buckets → **silently fails the 22 minimum.**
4. **[REASONING]** `pvalue` is the natural anomaly *severity* score and is
   directly usable as a ranking key — no invented scoring needed.

---

## F2. Current stack version

**[DOCUMENTED]** `elastic/elasticsearch@main/build-tools-internal/version.properties`:
```
elasticsearch     = 9.6.0
lucene            = 10.5.1
```
`main` is always the *unreleased* next version ⇒ **[REASONING]** latest released
GA as of Sep 2026 is in the 9.5.x line. Design target: **9.5+**.

---

## F3. EDOT OTel-native trace field names — GROUND TRUTH

**[DOCUMENTED]** Source: `elastic/elasticsearch@main/x-pack/plugin/otel-data/src/main/resources/`
(`index-templates/traces-otel@template.yaml`, `component-templates/traces-otel@mappings.yaml`,
`component-templates/otel@mappings.yaml`). These are the templates Elasticsearch
**actually installs** for EDOT data.

**Index pattern:** `traces-*.otel-*`  (priority 120, `data_stream: {}`)
**Index mode:** `logsdb`, sorted by `["resource.attributes.host.name", "@timestamp"]`

| Field | Type | Notes |
|---|---|---|
| `@timestamp` | `date` (millis) | `date_nanos` still pending upstream |
| `trace_id` | `keyword` | alias: `trace.id` |
| `span_id` | `keyword` | alias: `span.id` |
| `parent_span_id` | `keyword` | alias: `parent.id` |
| `name` | `keyword` | alias: `span.name` |
| `kind` | `keyword` | see **F4** for exact values |
| **`duration`** | `long` | **`meta.unit: nanos` — NANOSECONDS** |
| `status.code` | `keyword` | |
| `status.message` | `keyword` | |
| `attributes.*` | `passthrough` (priority 20), dynamic | span attributes |
| `resource.attributes.*` | `passthrough` (priority 40), dynamic | resource attributes |
| `scope.attributes.*` | `passthrough` (priority 30) | |
| `links.trace_id`, `links.span_id` | `keyword` | span links |
| `dropped_events_count`, `dropped_links_count` | `long` | |

### ⚠ Corrections to the current PROBE deck
- **There is no `transaction.duration.us`.** That is *classic Elastic APM / ECS*.
  OTel-native EDOT uses **`duration` in nanoseconds**. Dividing by `1000` gives µs;
  by `1_000_000` gives ms. Getting this wrong makes every latency number wrong by 10³.
- **`service.name`**: there is **no explicit alias** for it in
  `semconv-resource-to-ecs@mappings.yaml` — because none is needed. OTel's resource
  attribute is *already* named `service.name`, and `resource.attributes` is a
  **`passthrough`** field, which exposes subfields at the root.
  Canonical full path: **`resource.attributes.service.name`**.
  **[UNTESTED]** that ES|QL resolves the short form `service.name`. **Use the full
  path in all demo queries**; treat the short form as a convenience only. → **D0-3.**
- Aliases only exist where OTel and ECS names *differ*, e.g.
  `service.node.name` → `resource.attributes.service.instance.id`,
  `service.environment` → `resource.attributes.deployment.environment`.

---

## F4. Span `kind` values — the silent zero-rows trap

**[DOCUMENTED]** EDOT otel-native mode serializes span kind via
`elastic/../opentelemetry-collector-contrib@main/exporter/elasticsearchexporter/internal/serializer/otelserializer/traces.go`:
```go
first = w.writeStringFieldSkipDefault("kind", span.Kind().String(), first)
first = w.writeUIntField("duration", uint64(span.EndTimestamp()-span.StartTimestamp()), first)
```
and `open-telemetry/opentelemetry-collector@main/pdata/ptrace/span_kind.go`:
```go
func (sk SpanKind) String() string {
    case SpanKindUnspecified: return "Unspecified"
    case SpanKindInternal:    return "Internal"
    case SpanKindServer:      return "Server"
    case SpanKindClient:      return "Client"
    case SpanKindProducer:    return "Producer"
    case SpanKindConsumer:    return "Consumer"
}
```

⇒ **In EDOT otel-native data, `kind` is `"Server"` / `"Client"` / `"Internal"` /
`"Producer"` / `"Consumer"`.**

**NOT** `SPAN_KIND_SERVER` (that is `traceutil.SpanKindStr`, used only by the
*non*-OTel `nonOTelSpanEncoder`, which also writes `Kind`, `TraceId`, `Duration`
in **microseconds** — a different mapping mode entirely).
**NOT** `SERVER` (that is `spanKindToECSStr`, used only in **ECS** mapping mode,
which writes `span.kind`).

**Three different encodings exist for the same concept.** Writing
`WHERE kind == "SPAN_KIND_SERVER"` against EDOT otel data returns **zero rows,
with no error** — the worst possible stage failure.
→ **D0-4: confirm with one query before trusting any graph logic.**

Also confirmed here: `duration` = `EndTimestamp - StartTimestamp` where pdata
timestamps are **nanoseconds**. Independently corroborates F3.

---

## F5. `LOOKUP JOIN` — usable after `STATS`/`CHANGE_POINT` **locally**

**[DOCUMENTED]** `docs/reference/query-languages/esql/_snippets/commands/layout/lookup-join.md`
and `docs/reference/query-languages/esql/esql-lookup-join.md`.

```
stack: preview =9.0, ga 9.1+   |   serverless: ga
FROM <source_index> | LOOKUP JOIN <lookup_index> ON <join_condition>
```

Critical limitation, verbatim:
> "**Cross-cluster or cross-project** `LOOKUP JOIN` cannot be used after a command
> that runs on the querying cluster. This includes all pipeline-breaking commands —
> `STATS`, `INLINE STATS`, `SORT`, `LIMIT`, `TS_INFO`, and `METRICS_INFO` among them —
> and any command that only ever runs on the querying cluster, such as
> **`CHANGE_POINT`**, `FORK`, `FUSE`, `RERANK`, `COMPLETION`, `MMR`, and
> coordinator-side `ENRICH`."

**[REASONING] — de-risking read:** the restriction is explicitly scoped to
**cross-cluster / cross-project**. PROBE is single-cluster, so
`... | STATS ... | CHANGE_POINT ... | LOOKUP JOIN probe-service-graph ON ...`
**is legal**. This is the pivot the whole causal-ranking query depends on.
→ **D0-5: prove this exact shape on the cluster first. Highest demo risk.**

Other limitations that constrain the design:
- Lookup index **must** use `index.mode: lookup`; always **single-sharded**.
- "Currently, **only matching on equality** is supported." (The syntax section
  advertises `>=`/`<=` predicates as *preview* in 9.2 — the Limitations section
  contradicts it.) **Design decision: equality-only joins.** No range joins.
- Join field name must already exist in the query → plan `RENAME`/`EVAL` deliberately.
- Output row order is **not** guaranteed → **always `SORT` *after* the join.**
- Circuit-breaks on large lookup batches (~10k rows/batch).

---

## F6. `COMPLETION` — an LLM call *inside* ES|QL

**[DOCUMENTED]** `docs/reference/query-languages/esql/_snippets/commands/layout/completion.md`
```
serverless: ga  |  stack: preview 9.1.0, ga 9.3.0
```
Syntax (9.5+):
```esql
COMPLETION [column =] prompt WITH { "inference_id": "<id>" [, "timeout": "<duration>"] }
```
- Requires an **inference endpoint with task type `completion`**.
- Default output column is `completion`.
- **"Every row processed by the COMPLETION command generates a separate API call
  to the LLM endpoint."**
- 9.3+: **auto-limited to 100 rows by default** (`esql.command.completion.limit`).
- Default timeout 120 s (9.5+); 30 s on 9.1–9.4.
- Cross-cluster: runs on the cluster receiving the query.

**Why this matters strategically:** the LLM step can live *inside* Elasticsearch,
called from ES|QL, backed by Bedrock (F7). Elastic is then load-bearing for
reasoning orchestration, not just storage — and the compliance boundary
(Elastic + AWS only) is satisfied by construction.

**[REASONING]** The per-row API call semantics is also a *cost control*: PROBE must
reach `COMPLETION` with **exactly one row** (the ranked shortlist), so the cost per
incident is one Bedrock call regardless of log volume.

---

## F7. Bedrock as an Elastic inference endpoint — exact contract

**[DOCUMENTED]** `elastic/elasticsearch-specification@main/specification/inference/`
(`put_amazonbedrock/PutAmazonBedrockRequest.ts`, `_types/AmazonBedrockTypes.ts`).
```
@rest_spec_name inference.put_amazonbedrock
@availability stack since=8.12.0 stability=stable visibility=public
@cluster_privileges manage_inference
PUT /_inference/{task_type}/{amazonbedrock_inference_id}
```
`task_type` ∈ { `chat_completion`, `completion`, `text_embedding` }
`service` = `amazonbedrock`

`service_settings` (`AmazonBedrockServiceSettings`):
| field | req | notes |
|---|---|---|
| `access_key` | ✔ | AWS access key |
| `secret_key` | ✔ | AWS secret key |
| `region` | ✔ | "The region that your model or ARN is deployed in." |
| `model` | ✔ | base model ID **or ARN** of a custom model |
| `provider` | – | see list below |
| `rate_limit` | – | **default 240 requests/minute** |

`provider` supported values (verbatim):
- `amazontitan` — `text_embedding`, `completion`
- **`anthropic` — `chat_completion`, `completion`**
- `ai21labs`, `cohere` (+`text_embedding`), `meta`, `mistral`

`task_settings` (`AmazonBedrockTaskSettings`):
| field | notes |
|---|---|
| **`max_new_tokens`** | **`@server_default 64`** ⚠ |
| `temperature` | 0.0–1.0; not with `top_p`/`top_k` |
| `top_k` | anthropic/cohere/mistral only |
| `top_p` | not with `temperature` |

### ⚠ Two demo-killers hiding in here
1. **`max_new_tokens` defaults to 64.** A root-cause verdict will be silently
   truncated mid-sentence on stage. **Must set explicitly** (≥512). → **D0-6.**
2. Keys are **write-once**: "You need to provide the access and secret keys only
   once... After creating the inference model, you **cannot change** the associated
   key pairs. If you want to use a different key pair, **delete** the inference
   model and recreate it." Rotating a leaked key mid-event = delete + recreate.
3. `temperature` and `top_p`/`top_k` are **mutually exclusive** — sending both is a
   config error. For a *reproducible* stage demo we want `temperature: 0`, alone.

---

## F8. `FORK` — the head-to-head baseline, in one query

**[DOCUMENTED]** `docs/reference/query-languages/esql/_snippets/commands/layout/fork.md`
```
serverless: ga  |  stack: preview 9.1-9.3, ga 9.4+
FORK ( <processing_commands> ) ( <processing_commands> ) ...
```
- Runs multiple branches **over the same input data**, merges into one table.
- Adds a **`_fork`** discriminator column valued `fork1`, `fork2`, ...
- Branches may output different columns; same-named columns must share a type;
  missing columns become `null`.
- Row order preserved within a branch; use `SORT _fork` to group.
- **Max 8 branches. Only one `FORK` per query.**
- 9.4+: no implicit `LIMIT 1000` per branch (9.1–9.3 had one).

**[REASONING] Demo value:** PROBE-vs-threshold-baseline becomes **one query, two
branches, same input rows** — the fairest possible comparison and visually
self-evidently apples-to-apples. This is a far stronger stage artifact than two
separate queries the audience must trust were fed identical data.
