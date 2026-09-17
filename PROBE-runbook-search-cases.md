# PROBE — Runbook Search: How It Works, Every Case

How the Remediator finds a runbook. Fixed queries in a fixed order, then one small agent call. Worked example for every outcome.

**Order:** cache → stage 0 (exact) → stage 1 (hybrid) → ruled-out → confirm agent → hit or miss.

---

## 1. The order, and why

```
incident arrives (from Gate)
   │
   ├─ 0. Cache      in-process dict, fingerprint → runbook_id        ~0ms
   │       hit → GET by id → skip to 4
   │
   ├─ 1. Stage 0    ES|QL tool: exact term match on signature.*    ~10ms
   │       exactly 1 row → skip to 3
   │
   ├─ 2. Stage 1    ES|QL tool: FORK semantic + keyword, FUSE       ~100ms
   │       top-3 rows (may be 0)
   │
   ├─ 3. Ruled-out  ES|QL tool: ruled_out records for this symptom ~10ms   (always)
   │
   ├─ 4. Confirm    agent, one call, JSON yes/no + confidence        ~2s    (only if ≥1 candidate)
   │
   └─ 5. Decide     match && confidence ≥ τ → memory_hit
                    else                    → memory_miss → Correlator
```

Cheapest first. Most incidents in a real fleet are exact recurrences, so the common case should be nearly free. Semantic search is for the uncommon variant. The agent is last because it's the only step that costs seconds and the only step that can be wrong.

**Yes — the cache is checked before any search.** It's the first thing. It only skips the search, never the confirm.

---

## 2. The three tools

All parameterized ES|QL tools in Agent Builder. The workflow fills the parameters from the Detector/Gate output. The agent never calls them.

### Tool 1 — `rb_stage0_exact`
```esql
FROM probe-memory
| WHERE kind == "runbook"
  AND status != "demoted"
  AND signature.change_point    == ?change_point
  AND signature.metric          == ?metric
  AND signature.loudest_service == ?loudest
  AND signature.dependency      == ?dependency
| KEEP id, status, occurrences, failed_reuses, root_cause, steps, symptoms, ruled_out_before
| LIMIT 3
```
Four keyword equalities. A filter, not a search. Shard-cached on repeat.

### Tool 2 — `rb_stage1_hybrid`
```esql
FROM probe-memory
| WHERE kind == "runbook" AND status != "demoted"
| FORK
    ( WHERE MATCH(semantic, ?symptom) | SORT _score DESC | LIMIT 10 )
    ( WHERE signature.change_point == ?change_point
        OR  signature.loudest_service == ?loudest
        OR  signature.dependency == ?dependency
      | SORT occurrences DESC | LIMIT 10 )
| FUSE
| EVAL rank = _score - (failed_reuses * 0.1)
| SORT rank DESC
| KEEP id, status, occurrences, root_cause, steps, symptoms, ruled_out_before, _score
| LIMIT 3
```
Semantic on the symptom text + partial keyword on the signature, merged with RRF. Failed reuses pushed down.

Fallback if `FORK`/`FUSE` isn't on your version: one `_search` with an `rrf` retriever over the same two queries.

### Tool 3 — `rb_ruled_out`
```esql
FROM probe-memory
| WHERE kind == "ruled_out" AND MATCH(symptom, ?symptom)
| KEEP proposed.fault_class, proposed.service, incident, resolved_by
| LIMIT 10
```
Always runs. Result goes to the confirm agent on a hit and to the Correlator on a miss.

### The confirm agent
Fixed prompt, one candidate at a time (top-1 first; if rejected and there are more, the next):
```
Change point: {type} on {metric}, loudest {loudest}, dependency {dependency}
Candidate: {id} — {root_cause} — seen {occurrences}×, status {status}
Ruled out for this symptom: {ruled_out}
Does this runbook explain this change point?
JSON only: {"match": true|false, "confidence": 0..1}
```
`τ` is set on Day 2 from the variant runs, not guessed.

---

## 3. Every case

Fingerprint of the incoming incident in every example unless stated:
`step_change · p95_latency · loudest=cart · dependency=valkey-cart`
Symptom: *"cart latency spike, checkout slow"*

### Case A — nothing in memory (first ever run)
- Cache: empty. Miss.
- Stage 0: 0 rows.
- Stage 1: 0 rows — `probe-memory` has no runbooks.
- Ruled-out: 0 rows.
- Confirm: **skipped** (no candidate).
- Decision: `memory_miss`. Correlator runs cold. ~20s.
- After grading correct: Writer creates `upstream_dependency_latency__valkey-cart`, `status: candidate`. Cache is **not** populated — misses are never cached.

### Case B — exact recurrence (same fault, second time)
- Cache: populated? Only if a previous *hit* was confirmed. First recurrence after creation → cache miss.
- Stage 0: **1 row** — the runbook from Case A. Exact match on all four fields.
- Stage 1: skipped.
- Ruled-out: 0 rows.
- Confirm: `{"match": true, "confidence": 0.94}` ≥ τ.
- Decision: `memory_hit`. Correlator skipped. ~3s.
- Cache: `hash(fp) → upstream_dependency_latency__valkey-cart` written, TTL 10 min.
- Writer: `occurrences++` on the runbook, appends run id.

### Case C — third time, cache hit
- Cache: **hit**. GET runbook by id, ~2ms. `status != demoted` → proceed.
- Stage 0, Stage 1: **skipped**.
- Ruled-out: still runs.
- Confirm: still runs. `match: true`.
- Decision: `memory_hit`. ~2s total.

The cache saved the search, not the check. If the runbook had been demoted since, the GET would show it and the flow would fall through to stage 0.

### Case D — variant (same cause, different symptom)
Fingerprint: `step_change · error_rate · loudest=checkout · dependency=null`
Symptom: *"checkout errors, add-to-cart failing"*
- Cache: miss (different fingerprint).
- Stage 0: 0 rows — metric and loudest differ, dependency is null.
- Stage 1: semantic branch matches "cart… failing" to the runbook's `symptoms`; keyword branch matches `change_point == step_change`. FUSE puts `upstream_dependency_latency__valkey-cart` at rank 1, score 0.71.
- Ruled-out: 0 rows.
- Confirm: `{"match": true, "confidence": 0.82}` ≥ τ.
- Decision: `memory_hit`. ~4s.
- Writer: `occurrences++`, **appends the new symptom** to the runbook's `symptoms` list. Next time this symptom appears, stage 1 matches it more strongly.

This is the case that makes memory look like retrieval rather than a lookup table.

### Case E — near miss (looks similar, isn't)
Fingerprint: `step_change · p95_latency · loudest=cart · dependency=postgresql`
- Cache: miss.
- Stage 0: 0 rows — dependency differs.
- Stage 1: keyword branch matches change_point and loudest; semantic branch matches "cart latency". Runbook for valkey-cart comes back rank 1, score 0.58.
- Confirm: sees `dependency=postgresql` vs. runbook's `valkey-cart`. `{"match": false, "confidence": 0.22}`.
- Decision: `memory_miss`. Correlator runs, with the valkey-cart runbook passed as a near-match (context, not answer).
- After grading correct: new runbook `upstream_dependency_latency__postgresql`.

The near-miss is the demo beat that proves the memory can say no.

### Case F — memory says yes, and it's wrong
Same as Case B, but the injected fault was actually `resource_exhaustion / cart`.
- Stage 0 hit, confirm says match, `memory_hit`.
- Grader: `(upstream_dependency_latency, valkey-cart) != (resource_exhaustion, cart)`. **Wrong.**
- Writer: `failed_reuses++` on the valkey-cart runbook, `status → demoted`. Ruled-out record written: *symptom S, proposed valkey-cart runbook, incident run_019*. **The truth is not written.**
- Writer calls `remediator.invalidate(runbook_id)` → cache entry cleared.
- Next incident with this fingerprint: cache miss, stage 0 returns 0 rows (`status != demoted` filter), stage 1 may still surface it at reduced rank, confirm agent sees it's demoted and sees the ruled-out record. Almost certainly `memory_miss` → Correlator, now with the wrong answer marked.

### Case G — ruled-out shapes the answer
Fingerprint matches nothing. Symptom: *"cart latency spike"*.
- Stage 0, Stage 1: 0 rows.
- Ruled-out: **1 row** — `proposed: resource_exhaustion__cart, incident: PROBE-41, resolved_by: null`.
- Confirm: skipped.
- Decision: `memory_miss`. Correlator receives `ruled_out: [resource_exhaustion__cart]` in its evidence block. Its prompt says *"previously ruled out for this symptom: resource_exhaustion / cart."* The LLM's most tempting wrong answer is off the table before it reasons.

### Case H — stage 0 returns more than one
Two runbooks share the fingerprint (shouldn't happen if keys are `(fault_class, service)`, but can if two causes produce the same signature).
- Stage 0: **2 rows**.
- Rule: "exactly 1 → confirm" fails. Fall to stage 1, which ranks them by semantic + occurrences.
- Confirm on top-1. If rejected, confirm on top-2.
- Decision as usual.

### Case I — demoted runbook is the only match
- Stage 0: 0 rows (filter excludes demoted).
- Stage 1: 0 rows (same filter).
- Decision: `memory_miss`. The demoted runbook is **not** surfaced as a candidate. It's still in the index; a human can re-verify it via Jira, which resets it to `verified` and it starts matching again.

### Case J — confirm agent returns malformed JSON
- Treat as `{"match": false}`. Decision: `memory_miss`. Logged as `confirm_error`.
- Never treat a parse failure as a hit.

### Case K — human-created runbook (production path)
A human closed a Jira ticket with a cause the agent never proposed. Writer created `queue_backpressure__kafka` with `verified_by: [human]`, `signature` filled from that incident's Detector output.
- Next incident with that fingerprint: stage 0 hits it like any other runbook. There is no difference at search time between an injector-verified and a human-verified runbook. `verified_by` is shown on the evidence card; it doesn't change retrieval.

### Case L — watch or transient
- The Remediator **never runs**. The Gate didn't produce an incident. Nothing is searched.

---

## 4. Summary table

| Case | Cache | Stage 0 | Stage 1 | Confirm | Result | Time |
|---|---|---|---|---|---|---|
| A. Empty memory | miss | 0 | 0 | skipped | miss → cold | ~20s |
| B. Exact recurrence | miss | 1 | — | yes | **hit** | ~3s |
| C. Cached recurrence | **hit** | — | — | yes | **hit** | ~2s |
| D. Variant | miss | 0 | top-1 | yes | **hit** | ~4s |
| E. Near miss | miss | 0 | top-1 | **no** | miss → cold | ~22s |
| F. Hit but wrong | miss | 1 | — | yes | hit, graded wrong → demoted | ~3s |
| G. Ruled-out only | miss | 0 | 0 | skipped | miss, ruled-out passed to Correlator | ~20s |
| H. Stage 0 ambiguous | miss | 2+ | ranks them | on top-1 | as usual | ~4s |
| I. Only match is demoted | miss | 0 | 0 | skipped | miss | ~20s |
| J. Confirm malformed | any | any | any | error | miss | ~22s |
| K. Human-created runbook | miss | 1 | — | yes | **hit** | ~3s |
| L. Watch / transient | — | — | — | — | Remediator not called | — |

---

## 5. Why the agent doesn't search

Agent Builder offers two kinds of retrieval tool. **ES|QL tools** run a pre-defined, parameterized query — same query every time. **Index search tools** let the LLM write its own query from a natural-language request. The runbook search uses only the first kind, and the agent doesn't even call those — the workflow does, and hands the agent the rows.

Four reasons, all load-bearing:

**1. The hit rate has to be a number.** If the LLM decides how to search, two runs of the same fault can hit or miss depending on what query it wrote. "Memory hit rate: 8/10" stops being a property of the memory and becomes a property of the model's mood that day.

**2. Ablation needs a switch.** "Remediator off" must mean *one specific query didn't run*. You can't switch off a search the agent might or might not have decided to do.

**3. Cheap before expensive.** Stage 0 is a 10ms filter. Putting an LLM call in front of it — to *decide* whether to run the filter — makes the fast path slower than the slow path was supposed to be.

**4. The agent can't be trusted to filter.** `status != demoted` and `kind == "runbook"` are correctness rules. An LLM-written query might drop them. Elastic's own guidance: pre-defined ES|QL tools enforce rules an LLM might occasionally miss.

The agent's job is the last step: look at what the fixed queries returned and say whether it fits. That's a judgment call, so it goes to the model. Finding the candidates is not a judgment call, so it doesn't.

**Where free-form search is correct:** the human chat agent — "what do we know about cart latency?" — gets an index search tool over `probe-memory`. A person asking an open question is exactly when the LLM should write the query. That agent is off the graded path, so its nondeterminism costs nothing.

---

## 6. What the workflow looks like

```yaml
steps:
  - name: cache_lookup            # in-process, Python or workflow state
  - name: stage0                  # tool: rb_stage0_exact
    when: cache_miss
  - name: stage1                  # tool: rb_stage1_hybrid
    when: stage0.rows != 1
  - name: ruled_out               # tool: rb_ruled_out
  - name: confirm                 # agent, fixed prompt
    when: candidates > 0
  - name: decide
    hit:  path=memory_hit, cache.set(fp, id), render card, skip correlator
    miss: path=memory_miss, invoke correlator with candidates + ruled_out
```

Every branch is a condition on row counts or a JSON field. No branch is "ask the agent what to do next."

---

## 7. Judge lines

- *"How does it find the runbook?"* — Exact recurrence is a four-field filter, ten milliseconds. Variants are semantic over verified symptoms. The agent gets the result, not the query.
- *"Why not let the agent search?"* — Because then the hit rate would depend on what query it felt like writing. We need it to be a number.
- *"What if memory is wrong?"* — Case F. It's demoted, a ruled-out record is written, and the next time that symptom appears the wrong answer is already marked. We'll show it.
- *"Does it ever say no?"* — Case E. Similar signature, different dependency, the confirm step rejects it. We'll show that too.
