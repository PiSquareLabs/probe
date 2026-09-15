# Gates 3–6

---

# GATE 3 — EXPLAINABILITY
### Every module in ≤90 seconds, beginner-safe, every term defined.
**Rule: if you can't say it out loud without reading, it isn't done. Practise these aloud.**

### Module 1 — The detector (~60s)
> "Normally you'd alert when errors go above, say, 5%. The problem is that 5% is a number
> someone made up, and it's wrong for most services. So instead we ask Elasticsearch a
> different question: *for each service, when did its behaviour statistically change?*
>
> That's a command called **`CHANGE_POINT`**. You give it a number over time — here it's the
> error rate in ten-second buckets — and it tells you the moment the pattern shifted, and how
> confident it is. The confidence comes back as a **p-value**: a small number means 'this
> almost certainly didn't happen by chance.'
>
> No thresholds anywhere. A service that normally runs at 2% errors and jumps to 6% gets
> caught, and one that always sits at 8% doesn't get flagged forever."

**Terms:** `CHANGE_POINT` = ES|QL command [F1]. *Bucket* = a fixed time slice. *p-value* =
probability this is chance; **lower = more extreme** [F1, verbatim].

### Module 2 — The causal step (~75s) ⚠ *most important, and the corrected version*
> "Now ten services are broken and I need the one to blame.
>
> I have a second index that holds the **dependency graph** — who calls whom — which we built
> from the traces themselves. Then two joins.
>
> The first join asks, for each broken service: *what does this thing depend on?*
> The second asks: *are any of those also broken?*
>
> If a service depends on nothing that's broken, then nothing else explains why it's broken —
> **so it's the cause.** The frontend is broken, but it depends on product-catalog, which is
> also broken. So the frontend is a victim. Product-catalog depends on nothing that's broken.
> So product-catalog is the answer.
>
> That's it. No model, no scoring, no weights I had to tune."

**If asked about timing — answer immediately:**
> "I don't use timing to pick the cause, because I can't. It's a synchronous call: the
> frontend fails on the *same request*, milliseconds later. They're in the same bucket.
> Timing does a different job — it tells me whether I'm looking at one incident or three."

**Terms:** `LOOKUP JOIN` = ES|QL join to a lookup index [F5]. *Sink* = node with no
misbehaving dependency.

### Module 3 — Splitting concurrent incidents (~60s)
> "Three unrelated things break at once. Every alert tool gives you one big pile.
>
> The obvious fix is 'group the connected ones' — and that fails here, because the frontend
> calls *everything*. All three faults touch the frontend, so grouping merges them back into
> one. We hit that and designed around it.
>
> Instead: **each service with no broken dependency is its own incident.** Three such services
> means three incidents, three causes, three teams. The frontend shows up in all three —
> because it genuinely is a victim of all three. We don't force them apart; we split by
> *cause*, not by *service*."

### Module 4 — The agent (~60s)
> "Only now does a model get involved, and it never picks the answer — the query already did.
>
> Bedrock gets five rows: the candidates and the evidence. Its job is to confirm that against
> the evidence, pull the matching runbook, and write a fix a human can read.
>
> If the top two candidates changed in the same ten-second window, it can't order them — so it
> doesn't guess. It says 'confidence 58%', shows both, and stops.
>
> And because it only ever sees five rows, not five million, our cost per incident is about a
> cent and doesn't move when your log volume does."

### Module 5 — Self-grading (~45s)
> "We broke it on purpose, so we knew the answer first. Every verdict is compared to the flag
> we flipped.
>
> One rule that matters: if PROBE escalated instead of committing, **we don't count it as
> correct even when the guess was right.** It only gets credit for answers it stood behind."

---

# GATE 4 — COMPLIANCE ✅ PASS (after two removals)

**Requirement:** reasoning in Elastic Agent Builder + Workflows; models via Bedrock; Elastic +
AWS only.

| Component | Where it runs | Verdict |
|---|---|---|
| Detection (`CHANGE_POINT`) | Elasticsearch | ✅ Elastic |
| Causal ranking (`LOOKUP JOIN`) | Elasticsearch | ✅ Elastic |
| Incident splitting | Elasticsearch | ✅ Elastic |
| Runbook retrieval (ELSER + RRF) | Elasticsearch | ✅ Elastic — model runs *in-cluster*, no external call |
| Agent orchestration | Elastic Agent Builder | ✅ Elastic |
| Remediation workflow | Elastic Workflows | ✅ Elastic |
| LLM | **Amazon Bedrock**, via Elastic's `amazonbedrock` inference endpoint [F7] | ✅ AWS |
| Ticketing | Jira | ⚠ **output sink only** — not reasoning. Acceptable; say so if asked. |

### ❌ Two things REMOVED from the submitted stack — both were risks
1. **Jina AI** — a third-party embedding vendor **in the core retrieval loop**. This was a
   **compliance risk, not a scoring one**: it puts a non-Elastic, non-AWS service inside the
   reasoning path, and it sends your telemetry-derived text to a third party. **Replaced with
   ELSER**, which runs inside Elasticsearch and makes no external call at all.
2. **DiskBBQ** — not a compliance issue, but indefensible on merit: `bbq_disk` is a
   **disk-based ANN index for billion-scale, memory-constrained** vector search. The runbook
   corpus is ~10 documents. There is no good answer to "why?".

**Note on the boundary:** the LLM call is made *by Elasticsearch* to Bedrock via an inference
endpoint, so the reasoning orchestration never leaves Elastic. That is the strongest possible
reading of the rule, and worth stating explicitly.

**One honest caveat:** `build_service_graph.py` runs *outside* Elastic. It is **data
preparation, not reasoning** — it derives topology because ES|QL cannot self-join
`traces-*.otel-*` [F5]. Declare it rather than hide it.

---

# GATE 5 — DEMO RISK ✅ PASS (every fragile step has a rehearsed fallback)

| Fragile step | Risk | Pre-staged or live? | Fallback |
|---|---|---|---|
| `CHANGE_POINT` licence | 🔴 **fatal** | live | **None.** Trial must be started in minute 1 [F1]. |
| `CHANGE_POINT ... BY` (9.5+) | 🟠 | live | loop per service (+40 min) |
| `LOOKUP JOIN` after `CHANGE_POINT` | 🔴 | live | two-query shape — **already how `03` is written** |
| `FORK` + `CHANGE_POINT` | 🟡 | live | `05b` side-by-side; same story |
| Bedrock profile ID (ap-south-1) | 🟠 | pre-staged | get profile ID from console [F10] |
| `max_new_tokens` truncation | 🟠 | pre-staged | set 1024 [F7] |
| ELSER deployment | 🟠 | pre-staged | BM25-only retrieval |
| Service graph | 🟡 | **pre-staged** | `--seed` mode; **declare it** |
| <22 buckets | 🟠 | live | widen window to 30 min |
| Load generator died | 🔴 | live | restart + wait 4 min — **check at T−5** |
| Model/network down | 🟡 | — | pre-captured screenshots, **announced as screenshots** |

**Pre-staged vs live — say this unprompted:**
> "Pre-staged: the index templates, the dependency graph, and ten runbooks. Live: the fault,
> the detection, the ranking, the verdict and the grade. The interesting half is live."

---

# GATE 6 — SILENT WRONGNESS ⭐ *my own gate — the five above miss it*

**Why this gate exists.** Gates 1–5 ask "does it work?" and "what if it breaks?". Neither
catches the failure mode that actually loses hackathons: **the system returns a confident,
well-formatted, completely wrong answer, and nothing errors.** An exception is survivable on
stage — you say "that's a bug" and move on. A wrong answer delivered confidently, against
ground truth that proves it wrong, is fatal.

Every one of these was found during this work. Each returns **no error**.

| # | Silent failure | Symptom on stage | Detection | Status |
|---|---|---|---|---|
| 1 | `kind == "SPAN_KIND_SERVER"` in otel-native data | **Empty table.** Dead air. | pre-flight [4/7] | ✅ documented [F4] |
| 2 | `duration` read as µs when it is **ns** | Latency numbers **1000× wrong**, look plausible | sanity-check a p95 against Kibana APM | ✅ documented [F3] |
| 3 | `status.code != "Error"` filter | Drops every healthy span (field **absent** when Unset) → error rate ≈ 100% | compare `calls` to raw count | ✅ avoided via `CASE` |
| 4 | Service has **<22 buckets** | Service **silently skipped** — the real root can vanish | pre-flight [6/7] | ⚠ **fails unsafe** |
| 5 | **Messaging edge direction inverted** | Names the *victims* as root causes | scenario test | ✅ **found & fixed** (Gate 2, F3) |
| 6 | `max_new_tokens` = 64 | Verdict truncated **mid-sentence** | long-prompt smoke test | ✅ documented [F7] |
| 7 | Ground truth uses `productcatalog` not `product-catalog` | **Grade shows 0%** while the system is right | pre-flight [5/7] | ✅ documented |
| 8 | Stale service graph missing a new service | New service has no edges → always a "root" | compare graph node count to service count | ⚠ **fails safe** (over-reports) |
| 9 | Bucket ≥60s | **Below the 22 floor — returns nothing** | arithmetic | ✅ documented [F1] |

### The rule this gate produces
> **Every query must be proven to return the RIGHT answer on a KNOWN case before it is
> trusted on an unknown one.**

That is precisely what the flagd harness does — which is why the harness is not just the
pitch, it is the **engineering control**. Run one known fault end-to-end and confirm the grade
says *correct* before offering a judge a free flag choice.

**Two items still fail unsafe (#4, #8). Mitigation, 10 minutes:** run the bucket-count query
(pre-flight step 6) and a graph-coverage check immediately before the demo, and alert if any
service reporting spans is absent from the graph or under 22 buckets.
