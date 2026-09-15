# PROBE — Everything I Learned That I Didn't Know Before

One file. Only things this session actually *changed my mind about* — not a summary of
the design. Each entry says **what I assumed**, **what is true**, and **what it cost or
would have cost**.

Tags: **[DOCUMENTED]** primary source · **[MEASURED]** offline simulation ·
**[REASONING]** inference.

---

# PART 1 — Platform facts that contradicted the plan

### 1. `CHANGE_POINT` is a COMMAND, is licence-gated, and its `BY` clause is new
**Assumed:** an ES|QL *function* usable anywhere, on any cluster.
**True [DOCUMENTED]:** a *processing command*. **Requires a PLATINUM licence** — on Basic it
errors. GA 9.2; the **`BY` grouping clause is GA only in 9.5+**; multiple change points 9.6+.
Needs **≥22 values per series**, ignores beyond the first 1,000. Returns **every row** with
`type`/`pvalue` null except at the change point.
**Cost avoided:** without a trial licence there is no detector at all. Without `BY` you cannot
detect across all services in one query. Both are re-plan-the-day discoveries if found at H6.

> Source: `elastic/elasticsearch@main` doc *source* repo, not the website.

### 2. EDOT trace fields are not APM/ECS fields
**Assumed:** `transaction.duration.us`, `service.name`, `span.kind`.
**True [DOCUMENTED]:** OTel-native data streams `traces-*.otel-*` use **`duration` in
NANOSECONDS** (`meta.unit: nanos`). **There is no `transaction.duration.us`** — that is classic
APM. Canonical service field is **`resource.attributes.service.name`** (a `passthrough` field,
so the short form *may* resolve — unverified).
**Cost avoided:** every latency number wrong by **1000×**, and plausible-looking.

### 3. Span `kind` has THREE different encodings, and only one is right
**Assumed:** `SPAN_KIND_SERVER` (the OTel proto name).
**True [DOCUMENTED]:** otel-native mode writes `kind` = **`"Server"` / `"Client"` /
`"Internal"`** (Go `pdata.SpanKind.String()`). `SPAN_KIND_SERVER` belongs to the *non*-OTel
encoder; `SERVER` (with `span.kind`) belongs to **ECS** mode.
**Cost avoided:** `WHERE kind == "SPAN_KIND_SERVER"` returns **zero rows with no error**. The
worst possible stage failure — it looks like the system has nothing to say.

### 4. `status.code` is ABSENT on healthy spans
**Assumed:** every span has `status.code` ∈ {Ok, Error}.
**True [DOCUMENTED]:** the serializer **omits the field entirely when status is Unset**, which
is the default for most instrumentation.
**Cost avoided:** `WHERE status.code != "Error"` evaluates to null on those spans and **drops
every healthy span** → error rate computes as ~100%. Use `CASE(...)`, which falls through to
the default on a null condition.

### 5. The `LOOKUP JOIN` restriction is cross-cluster ONLY
**Assumed (with dread):** "cannot be used after pipeline-breaking commands" meant the whole
causal design was illegal.
**True [DOCUMENTED]:** the restriction is scoped to **cross-cluster / cross-project** queries.
Single-cluster `... | STATS | CHANGE_POINT | LOOKUP JOIN` is legal.
**Also learned:** lookup indices must be `index.mode: lookup` (always single-shard); **equality
matching only**; output order is **not** guaranteed, so `SORT` must come *after* the join.

### 6. ES|QL can call an LLM — and Bedrock can back it
**Didn't know existed:** `COMPLETION prompt WITH {"inference_id": ...}` runs an LLM **inside a
query**. `PUT _inference/completion/<id>` with `service: amazonbedrock`,
`provider: anthropic` points it at Bedrock.
**Why it mattered:** it keeps reasoning orchestration inside Elastic (compliance), and because
**`COMPLETION` bills one API call per ROW**, sending one row makes cost **flat in log volume**.
That single documented sentence is the whole cost argument.

### 7. Two Bedrock config traps
**[DOCUMENTED]** `max_new_tokens` **defaults to 64** → the verdict truncates mid-sentence.
`temperature` and `top_p`/`top_k` are **mutually exclusive**. Keys are **write-once** (rotate =
delete + recreate).
**[DOCUMENTED]** In **ap-south-1**, Claude is served via **cross-Region inference profiles** —
`model` must be a profile ID (`global.anthropic.…`), not a bare model ID, or Bedrock returns
*"on-demand throughput isn't supported."*

### 8. `FORK` exists and is perfect for a baseline comparison
**Didn't know existed:** runs multiple branches **over the same input rows**, tagging each with
a `_fork` discriminator. Makes "baseline vs PROBE" one query on provably identical data —
something two separate queries can never demonstrate.

---

# PART 2 — Design learnings (the ones that actually mattered)

### 9. ⭐ Temporal precedence barely works. Topology does all the work.
**Assumed:** two co-equal signals — *who broke first* + *who is upstream*.
**True [MEASURED]:** for a **synchronous** fault the root and its victims fail on the **same
requests, milliseconds apart**. Simulation confirmed it: every change point landed in the
**identical bucket**. Ordering root vs victim is **not noisy — it is impossible**, at any
bucket size.

**The mechanism was fine; the description was wrong.** The sink filter names the root; the sort
only orders *multiple independent roots*. `frontend` is excluded not because it changed later
but because **it depends on something also broken**.

> The better answer, and it is true: *"How do I know product-catalog broke first? I don't, and
> I don't need to."* Timing's real job is telling you whether it's **one incident or three**.

**Generalisable lesson:** when two signals are claimed, check whether one is doing all the
work. Here the weaker one was in the headline.

### 10. ⭐ Connected components is the obvious answer and it is wrong
**Assumed:** partition the anomalous dependency subgraph into connected components → N incidents.
**True [REASONING, confirmed MEASURED]:** `frontend` calls **everything**, so every blast radius
contains it and all components merge into **one**. The obvious algorithm produces *exactly the
false mega-incident PROBE exists to prevent.*
**Fix:** key incidents on their **root** (each sink = one incident) and **let blast radii
overlap**. `frontend` is genuinely a victim of all three.
**Lesson:** incidents are disjoint in **cause**, not in **services**. Forcing a partition
invents a lie.

### 11. 🔴 Async edge direction inverts the whole rule
**Found by static scenario testing.** The seed graph had `kafka → accounting`. But `accounting`
**consumes from** kafka, so **accounting depends on kafka**. For messaging, **call direction and
failure-propagation direction are opposite.**
**What it produced:** 4 incidents instead of 3, with `accounting` and `fraud-detection` named as
**root causes** and `kafka` — the real root — classified a **victim**. Wrong answer, in the
flagship demo, graded live as wrong.
**Lesson:** a dependency graph encodes *"who breaks when this breaks"*, not *"who calls whom."*
For sync RPC they coincide. For queues they invert.

### 12. 🔴 A safety heuristic fired on the exact case the product exists for
**Found by simulation.** The rule *"escalate if >40% of services are anomalous — probably
infra-wide"* looked sensible. Three genuine concurrent incidents make **9 of 18** services
anomalous → the rule escalated **all three** at confidence 0.45.
**Fix:** headcount doesn't indicate a systemic event; **unexplained** anomaly does. If the
roots' blast radii **cover** the anomalous set, trust the split regardless of headcount.
**Lesson, and it generalises well beyond this project:** *guardrails written against the normal
case will fire on your differentiating case, because your differentiator is by definition
abnormal.* Test every safety rule against your best feature.

### 13. 📏 Detection needs ~8 post-change buckets — regardless of bucket width
**Assumed [REASONING]:** 3–6 buckets ⇒ 20–60 s recognition.
**True [MEASURED]:** **8–9 buckets**, and the count is **invariant to bucket width**:

| bucket | buckets needed | latency |
|---|---|---|
| 5 s | 8 | **~40 s** |
| 10 s | 8 | ~80 s |
| 20 s | 8 | ~160 s |

**So bucket width is a linear latency dial.** Switched the demo to 5 s buckets — halves
recognition, still 240 buckets (far above the 22 floor).
**Lesson:** I was **2× optimistic** on a number I'd have said on stage. Latency estimates
derived from intuition about "enough evidence" are worth measuring even crudely.

---

# PART 3 — Competitive learnings

### 14. ⭐ "We causate, they correlate" is FALSE
**Assumed:** the category does correlation; PROBE does causation.
**True [DOCUMENTED]:** Datadog Watchdog RCA explicitly *"draws **causal relationships** between
symptoms"* with root-cause / critical-failure / impact separation. Dynatrace Davis traverses
Smartscape topology **deterministically**. Both market topology-aware causal RCA.
**Saying this on stage in front of a vendor-literate panel would have ended the pitch.**

### 15. …but the *true* claim is narrower and sharper
**[DOCUMENTED], verbatim:** Watchdog supports **four** root-cause types — version change,
traffic increase, EC2 instance failure, disk full — and *"**never** classifies degraded
application performance, such as higher latency or new errors, as the root cause."*
⇒ For a fault with **no deploy, no traffic spike, no infra event** (every flagd fault), Datadog
has **nothing in its taxonomy to name**. It reports a symptom and stops.
**Lesson:** the strongest competitive claim came from reading the competitor's docs *for what
they exclude*, not for what they promise. Marketing describes capability; **documentation
describes limits**, and limits are where differentiation actually lives.

### 16. The mechanism is well-trodden academically
TraceRCA (IWQoS 2021), MicroRCA, CausalRCA, TraceDiag, CHASE; survey arXiv 2407.01710.
**Lesson:** claiming novelty here would be caught. *Citing* it is a credibility gain — and the
honest differentiator ("those are offline pipelines; none of them ship") is stronger than the
false one.

### 17. The "Datadog isn't worth it unless you're locked in" attack doesn't hold
Watchdog RCA is bundled with APM and consumes telemetry an APM customer already sends, so the
marginal cost of getting RCA is ≈0 — the **opposite** of the claim. The defensible version is
about **telemetry cost**, not lock-in.
**Lesson:** pressure-test your attack lines as hard as your own claims. A false attack is more
dangerous than a weak one.

---

# PART 4 — Method learnings (most transferable)

### 18. ⭐ Simulation caught what review could not
Static review (2 passes, incl. an adversarial gate) found the topology and narrative problems.
**Simulation found two more that review missed** — the escalation-rule bug and the 2× latency
over-claim — because both only appear when you **run the thing end to end with numbers**.
**Lesson:** when you can't test the real system, simulate the *logic*. It is cheap, and the
bugs it finds are the kind that fail live rather than fail loudly.

### 19. ⭐ The dangerous failure is the CONFIDENT WRONG ANSWER, not the exception
I added a sixth gate for this after noticing the pattern. Nine failures in this design produce
**no error at all**: wrong `kind` literal → empty table; ns-vs-µs → 1000× wrong but plausible;
`status.code` filter → 100% error rate; <22 buckets → service silently skipped; inverted edge →
victims named as causes; `max_new_tokens: 64` → truncated verdict; wrong service-name spelling
→ grade reads 0% while the system is right.
**Lesson:** an exception on stage is survivable ("that's a bug"). A confident wrong answer,
graded wrong in front of the panel, is not. **Audit for silent wrongness separately from
auditing for failure.**

### 20. 100% accuracy is a warning sign
70/70 in simulation means the synthetic data is too clean, not that the system is excellent.
It is evidence the **logic** is sound, nothing more.
**Lesson:** when a result is perfect, go find the case that breaks it — that's what produced
the honest escalation demo (two services in the same bucket), which is worth more to a panel
than another success.

### 21. Doc *source* repos beat doc websites
`www.elastic.co` and `docs.aws.amazon.com` were blocked by egress policy. The workaround was
better than the original: `elastic/elasticsearch`, `elastic/elasticsearch-specification` and
`open-telemetry/*` on `raw.githubusercontent.com` contain the docs' **source**, the **API
spec**, the **actual index templates**, and **executable test fixtures** (`change_point.csv-spec`)
showing real inputs and outputs.
**Lesson:** for any question about exact behaviour, the test fixtures and the shipped mappings
are more authoritative than the rendered documentation, and usually more precise.

### 22. Conceding the ceiling buys credibility for everything else
The market answer scored 5/10 while it claimed enterprise appeal, and 8/10 once it said plainly
*"this is a wedge into the Elastic installed base, not a platform."* Same for prior art, and
for reporting **"4 of 5 correct"** instead of **"80% accuracy"** (a percentage from n=5 does not
survive one follow-up question).
**Lesson:** precision about limits reads as competence. Inflation reads as not knowing.

---

## The five I'd carry to any project

1. **When you claim two signals, check whether one is doing all the work.**
2. **Your safety heuristics will fire on your differentiating feature.** Test them against it.
3. **Audit for silent wrongness separately from failure.** Confident-and-wrong beats crash.
4. **Read competitors' docs for what they exclude**, not what they promise.
5. **Simulate the logic when you can't test the system.** It found the bugs review didn't.
