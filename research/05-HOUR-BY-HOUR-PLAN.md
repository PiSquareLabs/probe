# PROBE — The 9-Hour Build Plan
### There is no Day 0. Verification is hours 0–0:45 and it gates everything after it.

**The governing rule: at every cut line, a SMALLER THING THAT RUNS beats a bigger thing that
doesn't.** Ramnani and Pendyala hold 45 of 100 points and both score *demonstrated* depth.
Three queries running live outscore nine written perfectly.

**Two people minimum, working in parallel.** The lanes below assume
**BUILDER-A** (data + ES|QL) and **BUILDER-B** (agent + UI + pitch). A third person
takes the pitch/rehearsal lane from H6.

---

## ⏱ H0 – H0:45 · PRE-FLIGHT (both builders, nothing else runs until this passes)

**Do these in this order. Each is ~5 minutes. Any failure re-plans the day.**

| # | Check | Command | If it fails |
|---|---|---|---|
| 1 | **Licence** | `POST /_license/start_trial?acknowledge=true` | **Do this in minute 1.** `CHANGE_POINT` requires **platinum** [F1]. On Basic, PROBE has no detector. |
| 2 | **Version ≥ 9.5** | `GET /` | `BY` clause is 9.5+ [F1]. Below that → loop detection per service (costs 40 min at H3). |
| 3 | **Data flowing** | `FROM traces-*.otel-* \| STATS n=COUNT(*)` | Nothing else matters. Fix ingest before anything. |
| 4 | **`kind` literal** | `FROM traces-*.otel-* \| STATS n=COUNT(*) BY kind` | Expect `Server`/`Client` [F4]. If `SPAN_KIND_SERVER`, collector is in non-OTel mode → **every query needs rewriting and `duration` is µs not ns.** Catch this now, not at H7. |
| 5 | **Service names** | `... \| STATS n=COUNT(*) BY resource.attributes.service.name` | Copy the EXACT strings into ground truth. `product-catalog` not `productcatalog`. |
| 6 | **Bucket count ≥22** | see `esql/00-setup-indices.http` D0-7 query | <22 buckets ⇒ service silently skipped [F1]. Widen window or raise load. |

**H0:45 GO/NO-GO.** If 1–4 are green, proceed. If licence is Basic and no trial is
available, **stop and re-plan around an ML anomaly job or a z-score in ES|QL** — do not
discover this at H6.

---

## ⏱ H0:45 – H2:00 · LANE A: prove the two load-bearing queries

**This is the highest-risk block in the day. Nothing downstream works if these fail.**

1. **Detection** — run `01-detect-error-changepoints.esql` against live traces with **all
   flags off**. Expect *zero* change points. That's the correct baseline result and it proves
   the query runs.
2. Flip `productCatalogFailure → on`. Wait 3 minutes. Re-run. Expect `product-catalog` +
   its callers.
3. **THE CRITICAL ONE — `LOOKUP JOIN` after `CHANGE_POINT`** (D0-5). Create
   `probe-service-graph` with **three hand-written edges**, then run the join shape.
   - ✅ Works → the causal design is alive; continue.
   - ❌ Fails → **fall back immediately** to the two-query shape (`03-causal-rank.esql` is
     already written this way: Stage 1 writes to `probe-anomaly-current`, Stage 2 reads it).
     Cost: 20 minutes, not the design.

> **Do not build anything else until step 3 resolves.** It is the single assumption the
> whole project rests on.

## ⏱ H0:45 – H2:00 · LANE B (parallel): Bedrock + ground truth

1. `PUT /_inference/completion/probe-bedrock-claude` — **set `max_new_tokens: 1024`**, it
   defaults to **64** and will truncate the verdict [F7].
2. **Get the ap-south-1 cross-Region inference profile ID from the Bedrock console.** A bare
   model ID returns *"on-demand throughput isn't supported"* [F10]. **This will cost 20
   minutes if discovered late.**
3. Smoke-test with a long prompt. If it truncates, nothing downstream works.
4. Create `probe-ground-truth` + `probe-verdicts`. Write the 15 flagd flags → expected root
   service mapping by hand (it's 15 rows, `research/sources/otel/demo.flagd.json` has them).

---

## ⏱ H2:00 · ✂ CUT LINE 1 — scope locks here

**If the causal join works:** full plan continues.
**If it doesn't:** drop splitting (H4 block), keep detection + ranking + grading. You still
have a coherent, honest demo. **Decide now, not at H7.**

---

## ⏱ H2:00 – H3:30 · LANE A: the service graph, for real

Derive edges from actual traces rather than hardcoding — it takes ~40 minutes and it's the
difference between "we discovered the topology" and "we typed it in."

- **Preferred:** enable the collector `service_graph` connector → reads
  `traces_service_graph_request_total{client,server}` straight out of `metrics-*.otel-*`
  [DOCUMENTED, stability **alpha**, renamed from `servicegraph`]. **[UNTESTED]** whether EDOT
  bundles it — **timebox this to 20 minutes.**
- **Fallback (take it without regret):** one script — pull spans, join `parent_span_id`→
  `span_id` in memory, emit distinct `(caller, callee)` pairs, bulk-index into
  `probe-service-graph`. **Derived from real traces, just not in ES|QL.** Say that on stage.
- Then compute the **transitive closure** into `probe-service-reachability` (the graph is
  ~15 nodes; closure is trivial).

## ⏱ H2:00 – H3:30 · LANE B: Agent Builder

Create the tools (`POST /api/agent_builder/tools`, type `esql`, `?param` placeholders):
`probe.detect_anomalies`, `probe.rank_root_causes`, `probe.search_runbooks`.
**Skip `probe.split_incidents` until Lane A confirms splitting survives Cut Line 1.**

Write **8–10 runbooks** — one per demo flag, not all 15. `semantic_text` + `probe-elser`.
⚠ **ELSER must be deployed and downloaded** — kick that off at **H0:45**, it is not instant.
**If ELSER won't deploy by H3, drop to BM25-only retrieval** and say "hybrid is the
production shape; today this is lexical." Honest, costs ~1 point, saves an hour.

---

## ⏱ H3:30 – H5:00 · The end-to-end happy path

**Goal: one fault → one correct verdict → one grade on screen.** Nothing fancy.

Wire: detect → rank → agent confirms → write `probe-verdicts` → run `06-self-grade.esql`.

**H5:00 is the point of no return.** If the happy path isn't green here, **abandon splitting
and the dashboard** and spend the rest on making this one path bulletproof. One flawless
loop beats three broken ones — *"clear idea, solid framework, flawless execution"* is the
documented winning pattern at this event.

---

## ⏱ H5:00 · ✂ CUT LINE 2 — the honest triage

Remaining work in strict priority order. **Take them top-down; stop when the clock says so.**

| Pri | Item | Time | Points if done | Drop it if… |
|---|---|---|---|---|
| **1** | **Concurrent splitting demo** (3 flags at once) | 1.0h | **Originality anchor, +2–3** | H6 arrives first |
| **2** | **The honest failure case** (escalation path) | 0.5h | **+2 across 3 criteria** | never drop this — cheapest points in the day |
| **3** | **Incident Card dashboard** | 1.0h | +2.5 (Interface + Usability) | H7 arrives first |
| **4** | Latency detection query (`02`) | 0.5h | +0.5 | any pressure at all |
| **5** | `FORK` head-to-head (`05`) | 0.5h | +1 (great visual) | use `05b` instead — same story |
| **6** | Jira ticket output | 0.5h | +0.5 | almost always |

**Note on #2:** the escalation case is *0.5 hours* and moves Demo Quality, AI Implementation
and Problem Solving simultaneously. It is the best points-per-hour in the entire event.
**Build it before the dashboard.**

**Note on #6:** the repo already contains a working Jira integration (`backend/app/clients/jira.py`,
dual-path ticket creation, 58 passing tests). If ticketing is wanted, **wire the existing
code — do not rewrite it.**

---

## ⏱ H7:00 – H8:00 · Freeze and rehearse

**CODE FREEZE AT H7:00.** No new features. This is not negotiable — every hackathon loss is
a feature landed at H8:30 that broke the demo.

- Run the full demo script end to end, **timed**, three times.
- Rehearse **each fallback** in `04-DEMO-SCRIPT.md`, especially: FORK rejected, change point
  not found, Bedrock timeout.
- **Pre-capture screenshots of every successful step.** If something dies live, you show the
  screenshot and *say it's a screenshot*. Hiding it is what loses credibility, not having it.
- Reset all flags. Let the app run clean for 15 min so the baseline is healthy at showtime.

## ⏱ H8:00 – H9:00 · Pitch
- First 30 seconds: **the insight, not the architecture.** "Ten services break, one is to
  blame, and we'll show you our accuracy on faults we injected."
- Write the **4-sentence market answer** (buyer / budget line / wedge) — it is worth +3
  points and costs one hour of *writing*, not building. See `06-GATE1-SCORECARD.md` fix #1.
- Source the Bedrock price and the MTTR claim. 30 minutes, +1.5 points.

---

## What a 9-hour clock changes about the design

**[REASONING]** Three honest consequences the panel should hear rather than discover:

1. **The service graph is derived once, not continuously.** Correct anyway — topology changes
   on deploy timescales. Say it as a design decision, because it is one.
2. **The runbook corpus is ~10 documents, hand-written.** Do not dress this up as a corpus.
   Say: "ten runbooks, written by us; the retrieval is the part that generalises."
3. **Accuracy is measured over ~5 injected faults, not 100.** *Never present 5 runs as a
   statistical result.* Say: "five faults, four correct, one honestly escalated" — a count,
   not a percentage. A fabricated "80% accuracy" from n=5 is the kind of claim that ends a
   pitch under questioning; "4 of 5, and here they are" is unattackable.

## Deleted from the original plan, deliberately
- ❌ **DiskBBQ** — a billion-scale ANN algorithm on ~10 runbooks. Indefensible and costs time.
- ❌ **Jina AI** — third-party vendor in the core loop; a **compliance risk**, not a scoring one.
- ❌ **Kubernetes** — Docker Compose is faster and nothing in the demo needs k8s.
- ❌ **Deriving the graph in ES|QL** — ES|QL can't self-join `traces-*`; not worth the hour.
