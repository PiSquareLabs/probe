# PROBE — Competitive Teardown (sourced, for Q&A survival)

> **Read this first: one claim in the current deck is false and must be cut.**

---

## ❌ CUT THIS CLAIM: "Datadog correlates, PROBE causates"

**This is false and a judge who knows Datadog will end the pitch with it.**

**[DOCUMENTED]** Datadog, *Watchdog RCA*, https://docs.datadoghq.com/watchdog/rca/
(archived: `research/sources/competitive/dd_rca.txt`), verbatim:

> "The Watchdog AI engine identifies interdependencies between application
> performance anomalies and related components to **draw causal relationships
> between symptoms**."

> "A Watchdog Root Cause Analysis includes three components: **root cause,
> critical failure, and impact**."

> "**Impact**: Watchdog RCA also identifies services indirectly affected by the
> root cause. Any performance degradation listed in Impact is expected to recover
> once the Critical Failure is resolved."

Datadog explicitly markets **causal** RCA with root-cause-vs-symptom separation
and blast radius. So does Dynatrace:

**[DOCUMENTED]** Dynatrace Davis traverses the **Smartscape** topology to
establish causality "in a deterministic manner," and markets the *Visual
Resolution Path* as "based on **deterministic logic**, rather than probabilistic
and less reliable models."
(https://docs.dynatrace.com/docs/dynatrace-intelligence/root-cause-analysis/concepts —
*egress-blocked in this session, sourced via search snippets + vendor blog;
**[UNVERIFIED — confirm the exact Dynatrace wording before quoting it on stage]***)

⇒ **"We do causation, they do correlation" is not a defensible differentiator.**
Both leading vendors claim topology-aware causal RCA, and Dynatrace's claim is
architecturally very close to PROBE's.

---

## ✅ THE CLAIM THAT *IS* TRUE, AND IS SHARPER

**[DOCUMENTED]** Same Datadog page, verbatim — this is the decisive sentence:

> "Watchdog supports **four types of root causes**:
> - **Version changes**, as captured by APM Deployment Tracking
> - **Traffic increases**, as captured by hit rate metrics
> - **AWS instance failures**, as captured by Amazon EC2 integration metrics
> - **Running out of disk space**, as captured by system metrics"

> "**Watchdog never classifies degraded application performance, such as higher
> latency or new errors, as the root cause of an incident.** Datadog calls an
> initial symptom of degraded application performance a *critical failure*."

### What this means, precisely
Datadog's root-cause vocabulary is a **closed set of four infrastructure /
deployment state changes**. When a fault has **no deploy, no traffic spike, and
no infra event** — a config flip, a feature-flag change, a dependency degrading
under its own logic — Watchdog has **nothing in its taxonomy to name**. It reports
a *critical failure* (where degradation first appeared) and stops.

**This is exactly the flagd scenario.** Every one of the 15 OTel-demo faults
(F9) is a runtime behaviour change with no deployment and no infra event.

### The honest, sourced framing for stage
> "Everyone does causal RCA now — Datadog and Dynatrace both traverse topology.
> The real question is **what a tool is permitted to name as a cause**, and
> **whether you can ever check whether it was right**.
> Datadog's root-cause taxonomy is four infrastructure state changes, and it
> documents that it will never name a latency or error increase as a root cause.
> PROBE's answer is an open set — any service — and the evidence is temporal
> precedence plus topological position. And because we injected the fault, we can
> show you the grade."

**[REASONING]** This is stronger than the original claim *and* it is true *and*
it is sourced from the vendor's own docs. It also converts a Q&A landmine into a
prepared answer.

---

## ⚠ Where Datadog gets uncomfortably close — do not pretend otherwise

**"Critical failure: highlights where and how the root cause first (and most
directly) causes degraded application performance."**

That is a *first-symptom localisation*, and in many real faults the
first-degraded service **is** the culprit. A sharp judge will say: *"Isn't your
answer just Datadog's critical failure?"*

**Prepared answer [REASONING]:**
> "Partly — and that's the honest overlap. The difference is that Datadog frames
> the critical failure as a symptom still awaiting a root cause it can only name
> from four categories, and it reports one RCA per anomaly. PROBE treats the
> earliest-and-most-upstream anomalous service as the *answer*, attaches the
> evidence that makes it falsifiable — the change-point times and the dependency
> edges — and then grades that answer against a known injected truth. The claim
> isn't that nobody localises. It's that nobody shows you their hit rate on your
> system."

---

## The "vendor ships this next quarter" test

| PROBE component | Could a vendor ship it? | What actually remains |
|---|---|---|
| Change-point detection per service | **Already shipped.** Datadog/Dynatrace/New Relic all have anomaly detection. | Nothing. Don't claim it. |
| Topological causal ranking | **Already shipped** (Dynatrace Smartscape, Datadog RCA). | Nothing. Don't claim it. |
| Concurrent-unrelated incident **splitting** | Plausible, but they are structurally pulled the *other* way — their commercial promise is **alert-storm compression** (fewer incidents). Splitting one storm into N incidents is the opposite motion. | **[REASONING]** A real, defensible wedge — see §Splitting below. |
| **Self-grading against injected ground truth** | **They structurally cannot**, on customer production data: nobody knows the true cause, so there is no label to grade against. | **The strongest remaining claim.** |
| Per-customer compounding runbook memory | Vendors can ship retrieval; they can't ship *your* resolution history as a moat. | Weak-to-moderate. |

### The self-grading claim, stated honestly
**[REASONING]** Do **not** say "Datadog can't self-grade." They can and do
evaluate internally on benchmarks. Say this instead:

> "On *production* data nobody has labels — the true cause is whatever the human
> eventually wrote in the postmortem, if they wrote one. PROBE's harness works
> because we *cause* the fault, so the label exists before the answer does. That
> makes accuracy a measured number instead of a vendor claim. It's a property of
> the **evaluation environment**, not of our cleverness — and it's why we can put
> a hit-rate on screen and they put a testimonial."

That framing survives a hostile follow-up because it concedes the right thing.

---

## ⚠ Pressure-test requested: "Datadog isn't worth it unless you're already locked in"

**Verdict: DOES NOT HOLD. Do not say this on stage.**

**[REASONING]**, grounded in the documented facts above: Watchdog RCA is bundled
with Datadog APM rather than sold as a separate AIOps SKU, and its inputs
(APM metrics, deployment tracking, traces, infra metrics, log patterns) are the
same telemetry an APM customer already sends. So the marginal cost of *getting*
RCA once you have APM is ~zero, which is the opposite of the claim.

The defensible version of the point is about **cost of telemetry**, not lock-in:
Datadog bills on hosts, ingested/indexed spans and custom metrics, so
high-cardinality trace retention is where the bill grows.
**[UNVERIFIED — VERIFY BEFORE USE]** I could not reach `datadoghq.com/pricing`
under this session's egress policy to source specific list prices. **Do not quote
a Datadog price on stage without checking it that morning.** An unsourced pricing
attack is the single easiest way to lose credibility with a vendor-savvy panel.

**Safe formulation if asked about cost:**
> "I'm not going to claim a number I haven't checked today. The structural point
> is that these tools bill on ingested telemetry, so the RCA quality you get
> scales with what you're willing to pay to retain. PROBE runs the heavy
> statistics in ES|QL over data you already store, and only the ranked shortlist
> reaches a model — so our per-incident inference cost is flat in log volume."

---

## Prior art on the *mechanism* — surface it before the judges do

**[DOCUMENTED]** Trace-based causal RCA for microservices is a **well-developed
academic field**. Naming it is a credibility *gain*; claiming novelty here is a
credibility *loss*.

| Work | Core idea | Relevance |
|---|---|---|
| **TraceRCA** (Li et al., IWQoS 2021) — ieeexplore.ieee.org/document/9521340 | "a microservice with more abnormal and less normal traces passing through it is more likely to be the root cause"; trace anomaly detection → suspicious-set mining → ranking | Closest to PROBE's ranking intuition |
| **MicroRCA** | Topology graph of services+nodes, arcs = interactions; correlates KPI time series over it | Closest to PROBE's topological step |
| **CausalRCA** | Causal-inference-based fine-grained localisation | Stronger causal formalism than PROBE |
| **TraceDiag** (arXiv 2310.18740) | Adaptive, interpretable, efficient RCA | |
| **CHASE** (arXiv 2406.19711) | Causal hypergraph, multimodal | |
| Survey: arXiv **2407.01710** — *Failure Diagnosis in Microservice Systems* | Field survey | Cite this to show command of the literature |

**Prepared answer if a judge cites the literature [REASONING]:**
> "Yes — TraceRCA and MicroRCA established the core intuition years ago, and I'd
> point anyone to the 2024 failure-diagnosis survey. I'm not claiming the ranking
> is new. What's new here is the *packaging*: this runs as a handful of ES|QL
> queries inside a general-purpose search engine an ops team already runs, rather
> than as a bespoke ML service nobody deploys. Every one of those papers is an
> offline pipeline. None of them ship."

**[REASONING]** That answer converts the single most dangerous question in the
room into the strongest moment of the pitch — because it is true, and because
"research exists but doesn't deploy" is a thesis a technical panel already believes.

---

## Splitting: the one place novelty survives — with a caveat found in research

**[REASONING]** The commercial AIOps category is explicitly built on
**compression**: BigPanda, ManageEngine, Splunk et al. all market "collapse an
alert storm into a single prioritised incident." That is the **opposite** of
splitting one storm into N correctly-scoped incidents.

⚠ **But do not overclaim.** Datadog's docs say Watchdog "starts a root cause
analysis" **per APM anomaly** — i.e. it is *already* per-anomaly, not
globally-merged. **[UNVERIFIED]** whether Watchdog merges concurrent anomalies
into one RCA or emits several. **Do not assert that Datadog merges unrelated
faults into one incident** — I have not sourced it, and it may be wrong.

**Safe claim:** the baseline PROBE beats on stage is a **threshold-alerting
baseline**, which demonstrably produces N symptom alerts with no cause and no
grouping. That is a real, honest, demonstrable comparison. Beating *Datadog* is
a claim to make only if a same-cluster head-to-head is actually run, which it
will not be.
