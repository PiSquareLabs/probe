# PROBE — Component Contracts

What each module does, what it takes, what it returns, and what it must never do. Six modules, called in this order by the harness:

```
Detector → Gate → Remediator → Correlator → Grader → Writer
```

Each module is a file. None imports another's internals. The harness passes objects between them.

---

## 0. Shared objects

### `Candidate`
```json
{ "service": "cart", "signal": "p95_latency", "type": "step_change",
  "timestamp": "10:42:02", "pvalue": 0.0003, "z": 8.1, "tier": 2 }
```
Produced by the Detector. `type`/`pvalue` null and `tier: 1` when CHANGE_POINT didn't confirm.

### `Fingerprint`
```json
{ "change_point": "step_change", "metric": "p95_latency",
  "loudest_service": "cart", "dependency": "valkey-cart", "hops_to_cause": 1 }
```
Assembled by the harness from Detector output plus the Correlator's evidence queries. `dependency` and `hops_to_cause` are null until the deepest-span query has run. Used by the Remediator (stage 0) and stored on runbooks.

### `Diagnosis`
```json
{ "candidates": [ { "fault_class": "upstream_dependency_latency", "service": "valkey-cart", "confidence": 0.91 }, … ],
  "root_cause": "valkey-cart responding slowly; cart's client spans carry the delay",
  "symptom": "cart latency spike, checkout slow",
  "steps": [ "…", "…" ],
  "source": "memory_hit" | "correlator" }
```
Top-3 ranked. `fault_class` must be from the closed taxonomy; `unknown` is allowed.

---

## 1. Detector

**Question:** what changed, where, when, how sure?

| | |
|---|---|
| **In** | Nothing. Runs on a loop every 5–10s. |
| **Reads** | `traces-*`, `logs-*`, `metrics-*` |
| **Out** | `{ candidates: [Candidate], loudest, earliest, scan_at }` or `None` |
| **Uses LLM** | No |
| **Uses graph** | No |

**Does:**
- Tier 1: z-score scan (median/MAD), 5 signals × N services, persistence 2 scans → shortlist
- Tier 2: `change_point(service, signal)` on **every** shortlisted pair → type, timestamp, p-value

**Exposes:** `change_point(service, signal)` as a public function. The Correlator calls it for extra hops.

**Never:** names a cause, names a dependency, ranks, drops an unconfirmed candidate, reads the fault catalog or ground truth.

---

## 2. Gate

**Question:** should anyone care?

| | |
|---|---|
| **In** | Detector output + persistent `state` (transient counters) |
| **Reads** | Nothing from Elasticsearch |
| **Out** | `Decision { kind: incident \| transient \| watch, pattern: sustained \| intermittent \| null, trigger: [Candidate], supporting_evidence: [Candidate], evidence_timestamps: [] }` |
| **Uses LLM** | No |
| **Uses graph** | No |

**Does:**
- Symptom signals (`p95_latency`, `error_rate`) can open an incident; resource signals (`cpu`, `memory`, `log_errors`) cannot
- Tier-2 symptom + not recovered → `incident / sustained`
- Tier-2 symptom + `spike` / recovered → `transient`, counter++
- Third transient on same `(service, signal)` in 10 min → `incident / intermittent`
- Otherwise → `watch`
- On `incident`, attaches same-scan resource candidates as `supporting_evidence`

**Never:** runs on a watch or transient beyond writing `probe-runs`; opens a ticket on a resource-only anomaly.

**Stateful:** transient counters live for the process lifetime. Only module with cross-scan state besides the harness.

---

## 3. Remediator

**Question:** have we seen this before?

| | |
|---|---|
| **In** | `incident` Decision + partial `Fingerprint` + `symptom` string (templated from the change point if the Correlator hasn't run) |
| **Reads** | `probe-memory` only |
| **Out** | `{ path: memory_hit \| memory_miss, runbook, candidates: [top-3 runbooks], ruled_out: [], confirm: { match, confidence }, timings_ms: { stage0, stage1, stage2 } }` |
| **Uses LLM** | Stage 2 only — one small confirm call |
| **Uses graph** | No |

**Does:**
- Cache check — in-process `fingerprint → runbook_id` map (§7a). Hit → GET by id, straight to stage 2.
- Stage 0 — term filter on `signature.*` fields, `kind: runbook`, `status != demoted`. Exactly one hit → stage 2.
- Stage 1 — RRF over signature keyword match + `semantic` on symptoms. Top-3.
- Stage 2 — Bedrock: *"does this runbook explain this change point?"* → `match`, `confidence`. Above `τ` → `memory_hit`.
- Always retrieves `kind: ruled_out` records for the symptom, regardless of path.

**Never:** reasons about cause, reads telemetry, reads the catalog, sees ground truth, writes anything.

**Runs on:** every incident. Not on watches or transients.

---

## 4. Correlator

**Question:** why?

| | |
|---|---|
| **In** | `incident` Decision + Detector output + Remediator output (`candidates`, `ruled_out`) |
| **Reads** | `traces-*`, `service_graph.json`, `probe-changes` |
| **Calls** | `detector.change_point()` for extra hops |
| **Out** | `Diagnosis` with `source: correlator`, plus `evidence` block (below) |
| **Uses LLM** | One reasoning call |
| **Uses graph** | Yes — owns it |

**Evidence block built before the LLM call:**
```json
{
  "graph_rank":     [ { "service": "cart", "score": 0.82 }, … ],        // 0.6 × earliness + 0.4 × depended-on-by
  "deepest_span":   { "name": "HGET", "service": "cart", "kind": "CLIENT", "dependency": "valkey-cart" },
  "correlated":     [ "db.system=redis", "span.name=HGET" ],              // K-S / bucket_correlation
  "recent_changes": [],                                                   // probe-changes, last 5 min
  "extra_hops":     [ { "service": "valkey-proxy", "type": null } ],      // change_point() on loudest's callees
  "supporting":     [ { "service": "cart", "signal": "cpu", … } ],        // from Gate
  "near_matches":   [ … ],                                                // from Remediator stage 1
  "ruled_out":      [ { "proposed": "resource_exhaustion__cart", "incident": "PROBE-41" } ]
}
```

**Does:**
1. Graph ranking on Detector candidates
2. Deepest anomalous span; if `CLIENT`, read `db.system` / `peer.service` / `net.peer.name` → `dependency`
3. K-S / bucket correlation on span attributes
4. `probe-changes` lookup, 5-min window before earliest change point
5. One extra hop: `change_point()` on the loudest service's callees
6. Bedrock call with the evidence block → top-3 `(fault_class, service)`, root cause, symptom, steps
7. Validates `fault_class` against the taxonomy; anything outside → treated as `unknown`

**Never:** runs on a memory hit, sees ground truth, sees the catalog, grades itself, writes to memory.

**Runs on:** `memory_miss` only.

---

## 5. Grader

**Question:** was it right?

| | |
|---|---|
| **In** | `Diagnosis` + the harness's recorded truth `(fault_class, service)` + timings + baseline answers |
| **Reads** | Nothing |
| **Out** | one `probe-runs` doc |
| **Uses LLM** | **Never.** Ground truth comes from the injection only. |

**Does:**
- `correct_at1 = (top1.fault_class, top1.service) == truth`
- `correct_at3` — any of the three
- `abstained = top1.fault_class == "unknown"`
- Records: detection time, detection error (detected − injected), time to answer, `path`, tokens, cost, baseline answers (threshold / loudest / LLM-only / ml-classifier), `config` (FULL or ablation name)
- Null runs: `correct` = Gate returned `watch` or `transient`. An `incident` on a null window is a false positive.
- Must-not-open runs (resource-only flags): `correct` = Gate returned `watch`.

**Never:** consults a model, softens a mismatch, lets an out-of-taxonomy answer count.

---

## 6. Writer

**Question:** what do we keep?

| | |
|---|---|
| **In** | graded `probe-runs` doc + `Diagnosis` + `Fingerprint` |
| **Writes** | `probe-memory` (runbooks, ruled-out records), GitHub PR |
| **Uses LLM** | No — steps came from the Correlator |

**Does:**
- `correct_at1` and not abstained → `upsert_runbook((fault_class, service))`: create with `status: candidate`, or update in place (`occurrences++`, append symptom if variant, append run). Stitches any ruled-out record on this incident → `resolved_by`.
- Wrong and not abstained → `write_ruled_out(proposed = top1, incident, symptom)`. **The truth is never written on a miss.**
- Wrong and `path == memory_hit` → `failed_reuses++` on the reused runbook, `status → demoted`
- Abstained → writes nothing
- `config != FULL` (ablation) → writes nothing to memory, ever
- On recovery detected → `verified_by += recovery`
- On Jira Fixed → `verified_by += human`, `status → verified`; on Jira Rejected → ruled-out with `verified_by: human`
- **After any write to a runbook** → `remediator.invalidate(runbook_id)` so the fingerprint cache can't serve a stale entry

**Never:** writes on an ablation run, writes the truth on a miss, overwrites a human-edited runbook body, deletes a runbook.

---

## 7. Who reads what

| Store | Detector | Gate | Remediator | Correlator | Grader | Writer |
|---|---|---|---|---|---|---|
| `traces/logs/metrics-*` | R | | | R | | |
| `service_graph.json` | | | | R | | |
| `probe-changes` | | | | R | | |
| `probe-memory` | | | R | | | W |
| `probe-runs` | | W (watch/transient) | | | W | R |
| fault catalog / truth | | | | | R | |

The rightmost column of "truth" is the leakage boundary: only the Grader sees it. If any other module can reach the catalog or `fault.truth`, the number is fake.

---

## 7a. Runbook access — who reads, how fast, and the cache

### Who touches runbooks

| | Reads runbooks | Writes runbooks | How |
|---|---|---|---|
| **Remediator** | **Yes** | No | Stage 0/1/2 on `probe-memory` |
| **Writer** | No | **Yes** | `upsert_runbook`, ruled-out records, demotion |
| Correlator | No — secondhand only | No | Receives `near_matches` and `ruled_out` from the Remediator's output |
| Evidence card (Kibana) | Yes, display only | No | Renders the matched runbook |
| Agent Builder | Yes, chat only | No | `probe_memory_lookup` tool; never on the graded path |

One reader on the graded path, one writer. The Correlator never queries `probe-memory` itself — if it could, there'd be two retrieval logics and no way to attribute a hit or miss in the ablation.

### How fast the lookup is

| Stage | What it is | Cost | When it runs |
|---|---|---|---|
| **Cache** | In-process dict, fingerprint → runbook id | ~0ms | Every incident, first |
| **Stage 0** | Term filter on `signature.*` keyword fields | ~10ms | Cache miss |
| **Stage 1** | RRF: keyword + `semantic_text` | ~100ms | Stage 0 returned 0 or >1 |
| **Stage 2** | One small Bedrock call: "does this explain it?" | ~2s | Any candidate found |

Stage 0 is fast because every `signature.*` field is `keyword` and the query is a `bool.filter` — no scoring, no embeddings, and Elasticsearch's own shard request cache holds the filter result between identical queries. An exact recurrence never touches the vector index.

**Stage 2 is the floor.** The confirm call is the only part that takes seconds, and it runs on hits too — that's the price of never trusting a lookup blindly. Memory-hit total: ~2–4s, almost all of it Bedrock.

### The fingerprint cache

A tiny in-memory map inside the Remediator process:

```python
cache: dict[str, str]            # fingerprint_hash → runbook_id
cache_ttl = 10 minutes

def lookup(fp, symptom):
    key = hash(fp.change_point, fp.metric, fp.loudest_service, fp.dependency)
    if key in cache and not expired(key):
        rb = memory.get(cache[key])          # single doc GET by id, ~2ms
        if rb.status != "demoted":
            return confirm(rb, fp)           # still run stage 2
    # … stage 0 → stage 1 → stage 2 as before
    # on a confirmed hit: cache[key] = rb.id
```

Rules:
- **Stage 2 still runs on a cache hit.** The cache skips the *search*, not the *check*. A cached runbook that's been demoted or that no longer explains the change point must still be rejected.
- **Invalidated by the Writer.** Any write to a runbook (update, demote, verify) clears every cache entry pointing at that id. The Writer calls `remediator.invalidate(runbook_id)` — the only cross-module call in the system besides `change_point()`.
- **TTL 10 minutes.** Long enough to cover a demo's repeated injections; short enough that a stale entry can't survive a session.
- **Never caches misses.** A miss means the Correlator ran and possibly *created* a runbook — the next lookup must search.

**Honest sizing:** the cache saves ~10ms per exact recurrence. That's not the demo win — stage 2 still takes seconds. The real value is that a repeated fault doesn't re-run the retriever query at all, which matters at fleet scale when `probe-memory` has thousands of documents and the shard cache can't hold every filter. At demo scale it's a correctness-safe optimisation you can mention in one sentence, not a feature.

### What makes memory faster as it grows
More verified runbooks → more stage-0 exact hits → fewer stage-1 semantic searches. The expensive path gets *rarer* over time, not slower. That's the line for the pitch, and the cache is a footnote to it.

---

## 8. Timing per incident

| Path | Detector | Gate | Remediator | Correlator | Total |
|---|---|---|---|---|---|
| Cache hit (exact repeat) | ~15s | <1ms | ~2s (stage 2 only) | skipped | **~17s** |
| Memory hit (stage 0/1) | ~15s | <1ms | ~2–4s | skipped | **~18s** |
| Memory miss | ~15s | <1ms | ~0.2s | ~15–20s | **~35s** |
| Watch / transient | ~15s | <1ms | skipped | skipped | **~15s**, no ticket |

---

## 9. One line each

- **Detector** — what changed, and how sure.
- **Gate** — does it matter.
- **Remediator** — have we seen it.
- **Correlator** — why.
- **Grader** — was it right.
- **Writer** — what we keep.

---

## 10. Pushing more into Elasticsearch

Where each piece runs today, and what can move.

| Piece | Currently | Move to Elastic? |
|---|---|---|
| Remediator stage 0/1/ruled-out | ES\|QL tools | Already there |
| Remediator confirm | Agent Builder agent | Already there |
| Remediator cache | Python dict | **No** — it sits in front of Elastic by design |
| Correlator K-S / bucket correlation | Elastic aggregations | Already there |
| Correlator recent changes | `probe-changes` query | Already there |
| Correlator extra hop | `change_point()` tool | Already there |
| **Correlator graph ranking** | `service_graph.json` + Python | **Yes — §10.1** |
| **Correlator deepest-span traversal** | Python walks the trace tree | **Mostly — §10.2** |
| Correlator LLM | Agent Builder agent | Already there |
| **Tier 1 z-score (MAD)** | ES\|QL STATS + Python math | **Yes — §10.3** |
| Gate | Python | Workflow conditions; marginal gain, skip |
| Grader | Python | **No** — must stay outside the truth boundary |

After §10.1–10.3, the only Python on the graded path is the harness loop, the cache, the Gate's counters, and the Grader. Everything that *looks at data* is Elastic.

### 10.1 Graph → `probe-graph` index
The edge derivation already reads real spans. Write one doc per edge instead of a JSON file:
```json
{ "caller": "checkout", "callee": "cart", "count": 4812, "last_seen": "…" }
```
Then depended-on-by is a query:
```esql
FROM probe-graph
| WHERE callee IN (?anomalous) AND caller IN (?anomalous)
| STATS depended_on_by = COUNT_DISTINCT(caller) BY callee
```
`LOOKUP JOIN` that against the Detector's earliness per service, `EVAL score = 0.6*earliness + 0.4*depended_on_by`, sort. The graph becomes visible in Kibana next to APM's Service Map, which is drawing the same thing.

Cost: change the writer of `derive_service_graph.py` to bulk-index instead of dump JSON. Small. **Do this first.**

### 10.2 Deepest anomalous span → ES\|QL
True tree depth isn't needed. What's needed: among the loudest service's `CLIENT` spans, which jumped most after the change point, grouped by what they call.
```esql
FROM traces-generic.otel-probe
| WHERE service.name == ?loudest AND span.kind == "CLIENT"
  AND @timestamp > ?cp_time - 5 minutes
| EVAL phase = CASE(@timestamp >= ?cp_time, "after", "before")
| STATS p95 = PERCENTILE(duration, 95) BY phase, db.system, peer.service, span.name
| STATS before = MAX(CASE(phase == "before", p95, null)),
        after  = MAX(CASE(phase == "after",  p95, null))
        BY db.system, peer.service, span.name
| EVAL jump = after / before
| WHERE jump > 2
| SORT jump DESC | LIMIT 3
```
That names `valkey-cart` without walking anything. For `hops_to_cause`, `LOOKUP JOIN` on `parent.span.id` two levels up. Python only assembles the result. **Do this last** — the Python version works and this is the fiddliest.

### 10.3 Tier 1 z-score entirely in ES\|QL
MAD is a two-pass median; `INLINE STATS` makes it one statement:
```esql
FROM traces-generic.otel-probe
| WHERE @timestamp > NOW() - 10 minutes AND span.kind == "SERVER" AND probe.exclude != true
| STATS v = PERCENTILE(duration, 95) BY service.name, bucket = BUCKET(@timestamp, 10 seconds)
| INLINE STATS med = MEDIAN(v) BY service.name
| EVAL dev = ABS(v - med)
| INLINE STATS mad = MEDIAN(dev) BY service.name
| WHERE bucket >= NOW() - 30 seconds
| STATS recent = MEDIAN(v), med = MAX(med), mad = MAX(mad) BY service.name
| EVAL z = CASE(mad == 0 AND recent > med, 4.0, 0.6745 * (recent - med) / mad)
| WHERE z >= 3
```
The Detector becomes two ES\|QL statements and Python reads rows. Persistence (two consecutive scans) stays in Python — it's cross-scan state.

**Order:** 10.1 → 10.3 → 10.2.
