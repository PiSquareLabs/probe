# PROBE — Research & Design
### Forge the Future 2026 · Bengaluru finale · **9-hour build**

**Gate 1: 81.0/100** (top-3 contender band, no dead criterion) · **Gate 2: 7.6/10 technical**
· Gates 3–6: pass.

---

## Read in this order

| # | File | What it is |
|---|---|---|
| **1** | [`05-HOUR-BY-HOUR-PLAN.md`](05-HOUR-BY-HOUR-PLAN.md) | ⭐ **Start here on the day.** The 9-hour plan, two cut lines, code freeze at H7. |
| 2 | [`03-MECHANISM-DESIGN.md`](03-MECHANISM-DESIGN.md) | The winning mechanism and why the obvious version of it fails. |
| 3 | [`04-DEMO-SCRIPT.md`](04-DEMO-SCRIPT.md) | Minute-by-minute demo incl. ground-truth moment, honest failure case, "pick a flag". |
| 4 | [`01-VERIFIED-FACTS.md`](01-VERIFIED-FACTS.md) | Every Elastic/OTel/Bedrock fact with a primary source. **Settles arguments.** |
| 5 | [`07-ARCHITECTURE-SPEED.md`](07-ARCHITECTURE-SPEED.md) | Why recognition is fast, as an architectural argument. Latency budget. |
| 6 | [`08-PITCH-COST-MARKET.md`](08-PITCH-COST-MARKET.md) | 30-second opening, sourced cost, buyer and wedge, the ask. |
| 7 | [`02-COMPETITIVE-TEARDOWN.md`](02-COMPETITIVE-TEARDOWN.md) | Sourced competitor teardown + prior art. **Contains a claim you must cut.** |
| 8 | [`10-GATES-3-TO-6.md`](10-GATES-3-TO-6.md) | Spoken 90s explanations, compliance, demo risk, silent-wrongness. |
| 9 | [`09-GATE2-REALITY-CHECK.md`](09-GATE2-REALITY-CHECK.md) | Adversarial critique. Found two real defects. |
| 10 | [`06-GATE1-SCORECARD.md`](06-GATE1-SCORECARD.md) · [`11-GATE1-RESCORE.md`](11-GATE1-RESCORE.md) | Panel scoring, Q&A, ranked fixes. |

`esql/` — the queries. `artifacts/` — runnable pre-flight, ground truth, graph builder.
`sources/` — archived primary sources (Elastic doc source repos, OTel collector source).

---

## The five things that matter most

1. **Topology decides the root cause; timing does not.** A service with no misbehaving
   dependency is the cause. For a synchronous fault the root and its victims break in the
   *same* bucket, so "who broke first" cannot separate them — timing only tells you whether
   you're looking at one incident or three. *(Corrected in Gate 2 — the original pitch had
   this backwards.)*

2. **Connected components would break the flagship demo.** `frontend` calls everything, so all
   blast radii touch it and naive grouping merges unrelated faults into one mega-incident —
   the exact failure PROBE claims to fix. PROBE keys incidents on their **root**, and lets
   blast radii overlap.

3. **"We causate, they correlate" is FALSE — cut it.** Datadog and Dynatrace both ship causal
   RCA. The true, sourced claim: Datadog's root-cause taxonomy is **four** infrastructure
   state changes and it documents that it *never* names a latency or error increase as a root
   cause — so for a flag-flip fault it has nothing to name.

4. **Three silent killers.** `kind` is `"Server"` not `"SPAN_KIND_SERVER"` (zero rows, no
   error); `duration` is **nanoseconds** (there is no `transaction.duration.us`);
   `max_new_tokens` defaults to **64** (truncates the verdict). See Gate 6 for all nine.

5. **`CHANGE_POINT` requires a PLATINUM licence.** Start the trial in **minute one**.

---

## Changed from the submitted deck

| Was | Now | Why |
|---|---|---|
| Jina AI embeddings | **ELSER** (`semantic_text`) | Third-party vendor **inside the reasoning loop** — a compliance risk. ELSER runs in-cluster. |
| DiskBBQ | **ELSER + BM25 via RRF** | `bbq_disk` is billion-scale ANN; the corpus is ~10 docs. Indefensible on merit. |
| "LLM reasons over detector output to name the root cause" | **ES\|QL names it; Bedrock confirms** | Keeps the LLM off the critical path; makes cost flat in log volume. |
| Kubernetes | Docker Compose | Nothing in the demo needs k8s; it costs setup hours. |
| "8 of 10 at 40s mean" | **"4 of 5 correct, 1 escalated"** | A percentage from n=5 does not survive questioning. Report a count. |
| "20–40 min MTTR" | DORA restore-time + measured result | The original was unsourced. |

---

## Honest open risks

- **Nothing has run.** Every query is `[UNTESTED]`. This is a design, not a system.
- **Two silent failures remain unsafe:** a service under 22 buckets is skipped invisibly, and
  a stale graph makes a new service look like a root. 10-minute pre-demo checks mitigate both.
- **Bedrock prices are from secondary sources** — `aws.amazon.com` was blocked by this
  session's egress policy. Verify before quoting.
- **Dynatrace Davis's causal claim is sourced only from vendor material.** Assume it is
  genuinely competitive and do not attack it.
- **The concurrent demo is curated.** Offering free flag choice fixes this at zero cost.
- **`service_graph` connector in EDOT is unconfirmed.** Pre-materialising the graph is the
  safe choice.
