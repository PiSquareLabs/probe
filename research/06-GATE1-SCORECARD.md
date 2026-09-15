> Simulated panel. Role-based lenses from public professional profiles — not the actual
> judges' views or scores.

# Panel Verdict: PROBE
**73.5 / 100** · Strong top-3 technical contender carrying two dead criteria that are cheap to fix — and one existential gap.

## Scorecard
| Criterion | Weight | RR | SP | AB | SV | Final | Note |
|---|---|---|---|---|---|---|---|
| Originality | 10 | 7 | 7 | 7 | 6.5 | **7** | Splitting is novel; the RCA itself is not, and the team says so |
| AI Implementation | 15 | 12 | 12 | 12.5 | 11 | **12** | Uncertainty rule + eval harness are exactly right; nothing runs yet |
| Elasticsearch Integration | 15 | 12.5 | 13 | 12 | 12 | **12.5** | Genuine multi-capability depth, verified against source — but unexecuted |
| Technology Stack | 15 | 11 | 11 | 11.5 | 10.5 | **11** | Elegant boundaries, no-write-access posture; cost numbers unsourced |
| Problem Solving | 10 | 8.5 | 8 | 8.5 | 8 | **8.5** | Quantified before/after via self-grading; "20–40 min MTTR" unsourced |
| Market Potential | 10 | 5 | 5 | 5.5 | 4.5 | **5** | ☠ **DEAD.** No named buyer, no budget line, no wedge |
| Interface Design | 8 | 4.5 | 4.5 | 4 | 4.5 | **4.5** | ☠ **WEAK.** ES\|QL result tables are not an interface |
| Usability | 7 | 5 | 5 | 5 | 5 | **5** | Fits SRE workflow; platinum + 9.5 + setup is a real barrier |
| Demo Quality | 5 | 4 | 4 | 4 | 4 | **4** | Script is excellent; scored on a system that isn't built |
| Pitch Effectiveness | 5 | 4 | 4 | 4 | 4 | **4** | Insight lands in 30s; no clear ask |
| **Total** | **100** | | | | | **73.5** | Median-finalist band, one notch below contender |

---

## Individual verdicts

### Ravindra Ramnani — Elastic, Field Engineering lens
The strongest Elastic usage I've reviewed in this cohort on paper. `CHANGE_POINT` instead of
static thresholds is the right instinct and it's the thing I actually care about; two
`LOOKUP JOIN`s doing relational graph reasoning is not something most teams reach for;
`semantic_text` + RRF over the runbooks is the correct hybrid choice and they can explain
*why* hybrid — exact service names need lexical, paraphrase needs ELSER. The `index.mode:
lookup` and logsdb/TSDB decisions tell me someone read the mapping docs rather than guessing.
They know `duration` is nanoseconds and that `kind` is `"Server"`, which is the kind of detail
you only have if you've actually looked. **But none of it has touched a cluster, and
`CHANGE_POINT` needs a platinum licence they haven't confirmed.** I'd rather see three of
these queries running badly than nine written perfectly.

### Srinivas Pendyala — AWS, agentic architecture lens
Two things separate this from the field. First, the LLM is explicitly *not* allowed to
determine the root cause — ES|QL computes it and Bedrock confirms it. That's the correct
decomposition and most teams get it backwards. Second, there is a real answer to "how do you
know it's right": the flagd harness is an evaluation harness, and it refuses credit for
escalated incidents even when the guess was correct. That's unusually disciplined. Context
engineering is deliberate — one row reaches the model, never five million — and the
per-incident cost is structurally flat in log volume, which is the right property.
**What I can't accept is a cost model with no sourced prices.** "Illustrative rates" is not a
number. And routing Bedrock through Elastic's inference endpoint is elegant but means your
retry, timeout and rate-limit behaviour is Elastic's, not yours — I'd want to know you know that.

### Ankit Bose — NASSCOM, responsible-AI lens
The accountability posture is the best part of this submission and it was clearly designed in
rather than bolted on. No write access to the cluster it diagnoses. Proposal, not action.
Human approval recorded with approver and timestamp. Rollback captured *before* acting. Wrong
answers and escalations written to the same index as the right ones, so the audit trail
isn't flattering. The honest-failure case in the demo — where it says 58% and refuses the
point it would have won — is the single most trust-building thing here. **Where it falls
down is that a non-builder cannot see any of this.** The reasoning is legible only as ES|QL
result tables. If an SRE lead can't look at one screen and understand why PROBE blamed
product-catalog, the accountability exists in the architecture and not in the organisation.
Also: "20–40 minutes of MTTR" is an adjective wearing a number's clothes. Whose 20 minutes?

### Sehaj Virk — Sarvam, GTM lens
The insight is stated plainly and fast, which I appreciate: *many symptoms, one cause, and
here's the grade.* The splitting mechanism is the genuinely novel bit and the team is honest
that the causal ranking isn't — retiring the "we causate, they correlate" line before I could
attack it is the right call and it's the reason I'm not hostile. Unit economics are sound:
cost flat in log volume is exactly the shape that survives a cost-sensitive market.
**But this is a feature, not a business, and nobody has told me whose feature it is.** Who
signs the cheque? An SRE lead with what budget line? Why doesn't Elastic just ship this in
9.7 and make you a slide in their keynote? "The self-grading harness they can't replicate" is
an argument about *evaluation*, not about *product* — I need the product answer.

---

## Where the panel splits

**Elasticsearch Integration — Ramnani 12.5 vs Pendyala 13 (no split, but note the tension).**
Pendyala rates the design higher than Ramnani because he's scoring architecture; Ramnani is
scoring *demonstrated* depth and docks for zero cluster contact. **Ramnani leads — 12.5.**
The gap closes entirely the moment one query runs live.

**Originality — Virk 6.5 vs the technical judges' 7.** Virk sees a well-executed
recombination and is unmoved by the splitting nuance until he sees it fail-then-work on
stage. Ramnani and Pendyala credit the hub-topology insight as real invention because they
recognise that connected components is the obvious wrong answer. **Virk leads — 7.**
Virk's position: "clever is not the same as defensible."

**Problem Solving — Bose 8.5 vs Virk 8.** Bose credits the accountability design heavily.
Virk discounts it because accountability doesn't close deals. **Bose leads — 8.5.**

**The room's actual conversation:** Ramnani and Pendyala converge fast — this is the
technically deepest submission they've seen today, and between them they hold 45 points and
give it 35.5. That's a strong base and it means PROBE cannot be beaten on technique. Bose
brings it back: *"You've built something accountable that nobody can see."* Virk closes it:
*"And nobody's buying."* The verdict is that PROBE is a very good engineering project one
layer of product away from winning.

---

## Questions from the panel

**Ravindra Ramnani**
1. Your `03-causal-rank.esql` does a `LOOKUP JOIN` after a `CHANGE_POINT`. The docs list
   `CHANGE_POINT` among commands that run on the coordinating node. Walk me through what
   happens to that join at 50 million spans a day, and tell me whether you've actually run it.
2. You're detecting change points on error rate and p95 latency separately. `product-catalog`
   fails with a *lock contention* flag that raises latency without raising errors, while
   `frontend` shows errors and no latency change. Which query fires, and does your ranking
   still order them correctly when the two signals disagree?

**Srinivas Pendyala**
1. Your cost model says roughly 1.5 cents an incident using "illustrative Sonnet-class
   rates." Give me the real number for the model and region you're actually invoking, and
   tell me what the rate limit is — Elastic's Bedrock service defaults to 240 requests per
   minute. What happens to the eleventh concurrent incident?
2. Bedrock is reached through Elastic's inference endpoint, so Elastic owns the retry and
   timeout behaviour. `COMPLETION` defaults to a 120-second timeout. If that call times out
   mid-incident, what is the state of the Kibana case, and is the operation idempotent when
   the workflow retries?

**Ankit Bose**
1. PROBE tells an SRE that `payment` is the root cause with 91% confidence and they restart
   it. It was wrong — the real cause was a dependency PROBE never saw because it wasn't
   instrumented. Who is accountable, what in your system would have caught that, and what
   does the SRE see that would have made them doubt it?
2. Your accuracy number comes from faults *you injected*. Convince me that a number measured
   on self-inflicted, single-cause, cleanly-separated faults tells an enterprise anything
   about the messy multi-cause incident they actually have at 3am.

**Sehaj Virk**
1. Who signs the cheque? Name the function, the budget line it comes out of, and what they're
   currently spending it on instead.
2. Elastic ships root-cause ranking as a feature in 9.7 — it's an obvious extension of what
   they already have. What do you still own the next morning, and don't tell me about the
   grading harness, because that's a testing methodology, not a product.

---

## Ranked fixes — by points recoverable per hour

| # | Fix | Points | Effort | Owner |
|---|---|---|---|---|
| 1 | **Name the buyer and the wedge.** Platform/SRE lead in a 200+ engineer org; budget line is the existing observability spend; wedge is "runs on the Elastic you already pay for, no new vendor." Write 4 sentences into the pitch. | **+3.0** | 1h | pitch |
| 2 | **Build ONE Kibana dashboard: the Incident Card.** Root cause, confidence, the change-point timeline showing who broke first, the dependency edges traversed, the runbook, Approve/Reject. This single screen fixes the "reasoning is invisible" hit across Interface Design *and* Usability *and* Bose's verdict. | **+2.5** | 4h | frontend |
| 3 | **Source the Bedrock prices and the rate limit.** Closes Pendyala's most predictable question. Pure lookup, no build. | **+1.0** | 0.5h | backend |
| 4 | **Source the MTTR claim** (Google SRE book, or a cited industry incident-response report) or restate it as a measured observation from your own baseline run. Kills Bose's "adjective wearing a number's clothes." | **+0.5** | 0.5h | pitch |
| 5 | **Answer the multi-signal disagreement case** (Ramnani Q2) in one slide: error-rate and latency change points are unioned, and ties inside one bucket escalate. Already true in the design — just say it. | **+0.5** | 0.5h | pitch |
| 6 | **Prepare the "Elastic ships it" answer** (Virk Q2): the product is the *closed loop* — detection→verdict→approval→outcome→runbook memory that compounds per customer. Elastic ships primitives; PROBE ships the loop and the institutional memory. | **+0.5** | 0.5h | pitch |
| 7 | **RUN THE QUERIES ON A CLUSTER.** Every `[UNTESTED]` becomes `[VERIFIED]` and Ramnani's 12.5 goes to 14, Demo Quality 4→5. | **+3.0** | 6–10h | backend |
| 8 | Add `CATEGORIZE` over logs as a 3rd Elastic capability (log-pattern anomaly alongside trace anomaly) | +1.0 | 3h | backend |

**Fixes 1 + 3 + 4 + 5 + 6 = +5.5 points for 3 hours of writing.** That alone lands **79**.
Add fix 2 (4h) → **81.5**. Add fix 7 → **84.5**.

`[unfixable — reframe instead]` **Nothing is built yet.** With days left, do not attempt the
full system. Build *the demo path only*: the five queries, the graph index, the ground-truth
index, one dashboard. Reframe in the pitch as "this is the incident loop, running" rather
than "this is a platform."

---

## What I assumed
- **PROBE is currently a design, not a running system** — the repo contains an unrelated Jira
  connector app. → Cost: ~6 points across ES Integration, Demo Quality, Tech Stack. This is
  the single largest deduction and it is recoverable only by building the demo path.
- **No cluster has been touched**; every query is `[UNTESTED]` by the team's own admission.
  → Cost: ~2 points. Credit for honesty in labelling it, which is rarer than it should be.
- **Bedrock prices assumed illustrative**, since AWS docs were unreachable. → Cost: 1 point
  on Tech Stack.
- **The Astronomy Shop topology is assumed as documented in `.env`** rather than observed on a
  running cluster. → Cost: folded into the untested deduction.
