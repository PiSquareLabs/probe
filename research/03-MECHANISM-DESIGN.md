# PROBE — The Winning Mechanism

## 1. The insight, in one paragraph

When a microservice system breaks, every tool on the market can tell you *that*
ten services went bad. The reason none of them reliably tells you *which one is
to blame* is that they reason about **symptoms in isolation** — a service is
"anomalous" or it isn't. PROBE reasons about the **two things that make
causality decidable**: a cause cannot start after its effect (**temporal
precedence**), and a service whose own dependencies are all healthy cannot be a
victim (**topological position**). Put those together and the root cause is
whatever is left: *the anomalous service with no misbehaving dependency of its
own, that changed first.* That rule is lexicographic, not a weighted score — it
has no tunable magic numbers, it produces the same answer twice, and it is
falsifiable. And because it identifies incidents **by their root rather than by
a connected blob of services**, it naturally splits N simultaneous unrelated
faults into N correctly-scoped incidents, which is the exact case that makes
correlation-based tooling merge everything into one useless mega-alert. The LLM
never guesses the answer: ES|QL computes and ranks it over millions of spans,
and Bedrock is handed a five-row shortlist to confirm, explain and remediate.
Then PROBE grades itself against the flag we flipped, and puts the number on
screen.

---

## 2. Why the *obvious* version of this fails (the thing I'd have got caught on)

**The naive design is "connected components over the anomalous dependency
subgraph."** It is the first thing anyone writes. On the Astronomy Shop it is
**wrong**, and it fails silently.

`frontend` calls `ad`, `cart`, `checkout`, `currency`, `product-catalog`,
`recommendation` and `image-provider` **[DOCUMENTED** — service addresses in
`opentelemetry-demo/.env`, archived at `research/sources/otel/demo.env`**]**.
`frontend` is a hub. Inject three genuinely unrelated faults and all three
blast radii contain `frontend`, so connected components merges them into **one**
component and reports **one** incident — precisely the failure PROBE exists to
fix. Demoing that on stage would disprove the pitch live.

**PROBE's fix: identify an incident by its ROOT, not by a disjoint service set.**
- Every **sink** in the anomalous subgraph (anomalous service with no anomalous
  dependency) is a distinct root cause ⇒ a distinct incident.
- An incident's blast radius = anomalous services that transitively depend on
  that root **and** changed at/after it did.
- **Blast radii may overlap, and that is correct.** `frontend` genuinely is a
  victim of all three faults. PROBE labels it a **shared victim** instead of
  forcing a false partition.

> **The claim to make on stage:** "We don't claim the incidents are disjoint in
> *services* — the frontend really is broken three times. We claim they're
> disjoint in *cause*, and cause is what you page someone about."

**[REASONING]** This is the strongest single moment available, because the
nuance is only reachable by someone who actually ran it.

---

## 3. The pipeline

| Stage | Where it runs | What it does | Query |
|---|---|---|---|
| 1a | Elasticsearch (ES\|QL) | Per-service **error-rate** change points | `01-detect-error-changepoints.esql` |
| 1b | Elasticsearch (ES\|QL) | Per-service **latency** change points | `02-detect-latency-changepoints.esql` |
| 2 | Elasticsearch (ES\|QL + 2× LOOKUP JOIN) | **Causal ranking** → root candidates | `03-causal-rank.esql` |
| 3 | Elasticsearch (ES\|QL + reachability join) | **Split** into N independent incidents | `04-split-concurrent-incidents.esql` |
| 4a | Elasticsearch (ELSER + RRF) | Hybrid runbook retrieval | `07-runbook-hybrid-retrieval.esql` |
| 4b | Agent Builder → Bedrock | **Confirm**, explain, propose remediation, state confidence | §5 |
| 5 | Kibana Case + approval gate | Human approves before any action | §6 |
| 6 | Elasticsearch (ES\|QL) | **Self-grade** vs flagd ground truth | `06-self-grade.esql` |

Elastic is load-bearing in **four distinct capabilities**, not one:
statistical detection (`CHANGE_POINT`), relational graph reasoning
(`LOOKUP JOIN`), semantic + lexical retrieval (`semantic_text` + RRF), and
agent orchestration (Agent Builder + Workflows). That spans **observability and
search**, which is the specific thing the Elastic judge is scoring.

---

## 4. Why the LLM cannot cheat, and why the cost is flat

**[DOCUMENTED F6]** `COMPLETION` issues **one LLM API call per row**. PROBE
therefore reaches the model with **exactly one row**: the ranked shortlist.

- ES|QL aggregates **millions of spans** inside Elasticsearch.
- The model sees ≤5 candidate services + evidence + 3 retrieved runbooks.
- ⇒ **Bedrock cost per incident is constant in log volume.** Ten times the
  traffic changes the ES|QL scan, not the token count.

### Cost math
```
input  ≈ 2,000 tokens  (shortlist + evidence + 3 runbooks + instructions)
output ≈   600 tokens  (verdict + reasoning + remediation + confidence)
cost/incident = 2000/1e6 × P_in  +  600/1e6 × P_out
```
**[UNVERIFIED — VERIFY BEFORE QUOTING]** `aws.amazon.com` and
`docs.aws.amazon.com` are **blocked by this session's egress policy**, so I could
not source current Bedrock token prices. At *illustrative* Sonnet-class rates of
$3/M input and $15/M output this is **≈ $0.015 per incident**; at Haiku-class
rates roughly **$0.005**. **Confirm the real numbers on Day 0 and quote only
those.** The *structural* claim — flat in log volume — is sound regardless of
price, and that is the part that matters.

---

## 5. Agent Builder specification

**[DOCUMENTED]** Agent Builder tools are created at `POST /api/agent_builder/tools`
with tool types **`esql`** and **`index_search`**; ES|QL tools are parameterised
with `?param` placeholders.

### Tools exposed to the agent
| Tool | Type | Purpose |
|---|---|---|
| `probe.detect_anomalies` | `esql` | Stage 1a/1b — returns changed services + times |
| `probe.rank_root_causes` | `esql` | Stage 2 — returns ranked root candidates + evidence |
| `probe.split_incidents` | `esql` | Stage 3 — returns N incidents with blast radii |
| `probe.search_runbooks` | `index_search` | Hybrid ELSER+BM25 over `probe-runbooks` |
| `probe.get_service_deps` | `esql` | Neighbourhood of a service, for "show your work" |
| `probe.file_case` | workflow | Creates the Kibana Case (gated) |

### What the agent actually DECIDES (this is the Pendyala question)
The agent does **not** decide the root cause — ES|QL does. The agent decides:
1. **Whether the ES|QL verdict is trustworthy.** If the top-2 candidates have
   change points within one bucket (10 s) of each other, temporal precedence is
   not resolvable and the agent must say so rather than pick.
2. **Which tool to call next.** If `split_incidents` returns >1 incident, it must
   loop per incident instead of producing one narrative.
3. **Which runbook applies**, or that none does.
4. **Whether to act or escalate.**

### Defined behaviour when uncertain — the rule, not a vibe
```
confidence = f(evidence), computed BEFORE the model sees it:
  - margin  : t(candidate #2) − t(candidate #1), in buckets
  - support : # of anomalous services explained by this root
  - p-value : cp_pvalue of the root's own change point

ESCALATE (do not act) if ANY of:
  - margin < 1 bucket            → temporal order not resolvable
  - candidate is a leaf with 0 dependents  → nothing propagated; likely noise
  - no runbook scores above the RRF floor  → no known remediation
  - > 40% of services anomalous  → probably infra-wide, not a service fault
```
On escalation PROBE states **"confidence 60% — escalating"**, files the case
with its evidence and its *competing* hypotheses, and **takes no action**.

> **[REASONING]** This is the answer to "how do you know the output is right?":
> the flagd harness measures it, and the escalation rule means PROBE is scored
> on answers it actually committed to (see `06-self-grade.esql`, which refuses
> credit for escalated incidents *even when the guess was right*).

---

## 6. Accountability (the Bose points, and they are cheap)

Remediation is **proposed, never executed**:
1. PROBE writes a **Kibana Case** containing: predicted root cause, the
   evidence (change-point times, p-values, dependency edges traversed), the
   retrieved runbook, the proposed command, and the confidence.
2. The case sits in **`awaiting_approval`**. No action is taken.
3. A human approves or rejects. The approval, the approver and the timestamp are
   appended to the case.
4. Only then does the Workflow execute, and it records a **rollback command**
   captured from the runbook's `rollback` field *before* acting.
5. Every PROBE verdict is written to `probe-verdicts` — including the wrong ones
   and the escalated ones — so the audit trail is complete rather than flattering.

> **Stage line:** "PROBE has no write access to the cluster it diagnoses. It can
> open a case and it can wait. That's deliberate — an agent that can restart
> your payment service at 3am is a liability, not a feature."

---

## 7. Honest statement of what is and isn't novel

**Not novel — do not claim it:** trace-based causal RCA. TraceRCA (IWQoS 2021),
MicroRCA, CausalRCA, TraceDiag, CHASE. Survey: arXiv 2407.01710. Datadog and
Dynatrace both ship topology-aware causal RCA. See `02-COMPETITIVE-TEARDOWN.md`.

**Defensible:**
1. **Root-keyed incident splitting with overlapping blast radii.** The
   commercial category is built on *compression* (collapse the storm into one
   incident); splitting is the opposite motion.
2. **The self-grading harness.** Not because it is clever, but because it is
   only possible when you *caused* the fault. On production data nobody has the
   label. This is a property of the evaluation environment, honestly stated.
3. **It runs as ES|QL in a search engine ops teams already operate**, not as a
   bespoke offline ML pipeline. Every paper above is offline. None of them ship.
