# PROBE — Architecture for Fastest Recognition and Remediation

> The claim is **not** "our model is smarter." It is: **PROBE's time-to-named-cause is
> bounded by the detection window, because causation is computed in the same pass as
> detection — and no language model sits on the critical path to the verdict.**
> Everything below is an argument about *where work happens*, which is the only kind of
> speed claim that survives a senior architect.

---

## 1. The critical path, stage by stage

```
  t0  fault begins (flagd flips)
   │
   ├─ INGEST  ────────────────────────────────────────────────┐
   │   OTel SDK BatchSpanProcessor    ~0.2–5 s  (tunable)      │
   │   Collector batch processor      ~0.2–1 s  (tunable)      │  ~2–7 s
   │   ES index + refresh_interval    ~1–5 s    (tunable)      │
   │                                                            ┘
   ├─ RECOGNITION  ───────────────────────────────────────────┐
   │   bucket close + post-change evidence                     │
   │     CHANGE_POINT must see enough post-change points       │  ~20–60 s
   │     to call it: ~3–6 buckets × 10 s                       │  ◄── DOMINANT TERM
   │   detection query                      < 1 s              │
   │   causal ranking query (2 LOOKUP JOINs)  < 1 s            │
   │                                                            ┘
   ▼
  t1  ROOT CAUSE NAMED, WITH EVIDENCE      ≈ 30–70 s      ◄── no LLM has run yet
   │
   ├─ EXPLANATION / REMEDIATION  ─────────────────────────────┐
   │   hybrid runbook retrieval (ELSER + BM25 + RRF)  ~50–150 ms│  ~2–5 s
   │   Bedrock confirm + write remediation             ~1–4 s  │
   │                                                            ┘
   ▼
  t2  PROPOSED FIX + ROLLBACK, AWAITING HUMAN     ≈ 35–75 s
```

**[REASONING]** These are budgets derived from the mechanism and from documented
defaults, **not measured results**. The *structure* is the claim; the numbers are
[UNTESTED] until the cluster runs.

### The one number that matters, and its honest tradeoff
Recognition is dominated by **how many post-change buckets `CHANGE_POINT` needs**.
That is a real, tunable, explainable knob:

| Bucket | Buckets in 20 min | Detection latency | Risk |
|---|---|---|---|
| 5 s | 240 | **~20–30 s** | noisier; low-traffic services fall under the 22-value floor [F1] |
| 10 s | 120 | ~30–60 s | **the demo default — safe margin above the 22 floor** |
| 30 s | 40 | ~90–180 s | very stable, too slow to impress |
| 60 s | 20 | ✘ **fails** | **below the 22-value minimum — silently returns nothing** [F1] |

> **Say this on stage when asked "how fast":** *"Thirty to sixty seconds, and I can tell you
> exactly what sets it — the change-point detector needs a handful of buckets after the
> change to be sure. Shrink the bucket and I go faster and noisier. That's the whole
> tradeoff, and it's a dial, not a mystery."*

That answer is worth more than any number, because it demonstrates the team knows what
governs its own latency.

---

## 2. The four architectural decisions that create the speed

### D1 · Causation is a by-product of detection, not a second stage
Most pipelines are **detect → then correlate**. Correlation is a second pass over the data,
usually in a different system, and it is where the minutes go.

PROBE's detector emits **`(service, change_time, p_value)`**. The *change_time* is precisely
the ordering that temporal precedence needs. So the causal step is not a new analysis — it is
a `SORT` plus two joins against a precomputed graph. **The expensive input to causation was
already produced by detection, for free.**

**[REASONING]** This is the core speed argument and it is structural, not an optimisation.

### D2 · The LLM is off the critical path to the verdict
The root cause is named by ES|QL. Bedrock is invoked *after* `t1`, to confirm, explain and
draft remediation.

**Consequence:** PROBE's recognition latency is **independent of model latency, token
throughput, rate limits, and context size.** A product that *generates* its diagnosis with an
LLM has a latency floor of model latency, and degrades under exactly the conditions that
matter — a big incident with lots of context.

⚠ **[DOCUMENTED F7]** Elastic's Bedrock service defaults to **240 requests/minute**. Because
the LLM is off the critical path, hitting that limit delays *explanations*, not *detection* —
PROBE degrades gracefully into "here is the ranked cause and the evidence, the narrative is
queued." That is a designed degradation mode, and it is worth saying.

### D3 · The heavy statistics run inside the storage engine
`CHANGE_POINT` and the aggregation run in Elasticsearch, over data already indexed. Nothing
is exported to a correlation service. The only thing that crosses a network boundary after
`t1` is a five-row shortlist.

**Consequence:** cost and latency are **flat in log volume** [F6 — `COMPLETION` is one API
call per row, and PROBE sends one row]. Ten times the traffic changes the scan, not the
token count.

### D4 · Topology is precomputed, not traversed
The dependency graph and its transitive closure live in **`index.mode: lookup`** indices
(single-shard, by definition [F5]). The causal step is therefore a **hash join**, not a graph
walk. Topology changes on deploy timescales, so recomputing it per incident would buy nothing
and cost seconds.

---

## 3. Why the competitors are structurally slower *at naming a cause*

**Be careful here — the honest claim is narrow. See `02-COMPETITIVE-TEARDOWN.md`.**

| | Detects fast? | Names the cause of a **flag-flip / config** fault? |
|---|---|---|
| Threshold alerting | ✅ instant | ❌ **never** — fires on symptoms; a human correlates (the 20–40 min) |
| **Datadog Watchdog RCA** | ✅ | ❌ **structurally cannot.** **[DOCUMENTED]** Root causes are a closed set of **four** state changes — version change, traffic increase, EC2 instance failure, disk full — and *"Watchdog **never** classifies degraded application performance, such as higher latency or new errors, as the root cause."* A feature-flag fault matches none of the four, so it reports a *critical failure* (a symptom) and stops. |
| Dynatrace Davis | ✅ | ✅ traverses Smartscape topology deterministically — **[UNVERIFIED]**, sourced only from vendor material. **Assume it is genuinely competitive here and do not attack it.** |
| LLM-first "AI SRE" tools | varies | ⚠ bounded below by model latency; degrades as incident context grows |

### The defensible sentence
> "For a fault with **no deploy, no traffic spike and no infrastructure event** — which is
> most config and dependency faults — Datadog documents that it will not name a cause at all.
> It hands you the first symptom. We hand you the service, the two pieces of evidence that
> implicate it, and the fact that we were graded on it."

**Do not say "we're faster than Datadog."** You have not benchmarked it and cannot.
Say **"we name a class of cause their documented taxonomy excludes."** That is sourced, and
it is stronger.

---

## 4. Remediation speed, and the part that compounds

```
verdict ──► hybrid runbook retrieval ──► proposed fix + rollback ──► HUMAN GATE ──► execute
             ELSER + BM25 via RRF          from runbook.remediation      approve      workflow
             ~50–150 ms                     + runbook.rollback           recorded     + rollback
                   ▲                                                         │
                   └──────────── outcome written back ◄──────────────────────┘
                                 (what actually worked, on THIS estate)
```

**Why hybrid and not pure vector:** service names, error codes and flag names are **exact
tokens** — BM25 nails them. Paraphrase ("the catalogue service is throwing") needs ELSER.
RRF merges both without a tuned score threshold, which is the part that would otherwise need
hand-waving. **[DOCUMENTED]** ELSER's sparse term expansion is **inspectable**, so the UI can
show *which terms matched* — retrieval that can be shown is retrieval an SRE will trust.

**The compounding asset [REASONING]:** every approved (or rejected) remediation is written
back with its outcome. The corpus becomes a record of *what worked on this estate*. A vendor
can ship better retrieval; a vendor cannot ship **your** incident history. This is the honest
version of the moat — and it is the answer to *"what do you own after Elastic ships this?"*

---

## 5. Degradation modes (Pendyala will ask; have these ready)

| Failure | What PROBE does | Recognition still works? |
|---|---|---|
| Bedrock times out / rate-limited | verdict + evidence still published; narrative queued | ✅ **yes** — LLM is off the path |
| ELSER not deployed | falls back to BM25-only retrieval | ✅ yes, worse ranking |
| Service graph stale (new service) | new service has no edges ⇒ treated as a sink ⇒ becomes a *candidate* | ✅ **fails safe** — over-reports rather than hiding a cause |
| Fewer than 22 buckets for a service | that series is skipped **silently** [F1] | ⚠ **fails unsafe** — mitigate by alerting on low bucket counts |
| Two services change in the same bucket | margin < 1 bucket ⇒ **escalate**, both hypotheses shown | ✅ by design |
| >40% of services anomalous | classified as infra-wide, not a service fault ⇒ escalate | ✅ by design |

**The one that fails unsafe is worth admitting before it's found.** A low-traffic service
that never reaches 22 buckets is invisible to the detector. Mitigation: a standing check on
bucket counts per service (it is the same query as pre-flight step 6).

---

## 6. What this architecture is *not* good at (say it before they ask)

1. **First-occurrence faults with no runbook.** Retrieval returns nothing useful; PROBE
   escalates. It does not invent a remediation.
2. **Faults in uninstrumented components.** If it does not emit spans, it is not in the
   graph, and its victims look like sinks — PROBE will name the wrong service. **This is the
   honest failure mode and the right answer to Bose's accountability question.**
3. **Slow-burn degradation.** `CHANGE_POINT` finds *changes*. A service that has been slowly
   degrading for a week has no change point. Different problem, different tool.
4. **Single-cause assumption per incident.** Two simultaneous faults in the *same* service
   are reported as one incident.
