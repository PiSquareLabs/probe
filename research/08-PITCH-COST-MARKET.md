# PROBE — Pitch, Cost and Market
### Closing the two dead criteria from Gate 1 (Market 5/10, and the unsourced cost number)

---

## 1. The first 30 seconds (insight before architecture)

> "When a microservice system breaks, ten services light up and one of them is actually to
> blame. Every tool you own tells you about the ten. Finding the one is a human being reading
> logs for twenty minutes.
>
> PROBE names the one, and it proves it with two things a language model can't argue with:
> **who broke first**, and **who is upstream of everyone else that broke**.
>
> Then — and this is the part I actually want you to watch — we break our own system on
> purpose, so we know the right answer *before* PROBE does. Everything you're about to see
> gets graded live, on screen, including the one it gets wrong."

**No architecture in the first 30 seconds.** No "Agent Builder", no "ES|QL", no "Bedrock".
Those are the *answer* to the first question, not the opening.

---

## 2. Cost per incident — now sourced

**[DOCUMENTED — secondary sources; `aws.amazon.com` is blocked by this session's egress
policy, so VERIFY on the AWS Bedrock pricing page before quoting]**

| Model | Input $/M tok | Output $/M tok |
|---|---|---|
| Claude Sonnet 5 (global CRIS) | $2.00 | $10.00 |
| Claude Haiku 4.5 (global CRIS) | $1.00 | $5.00 |

Also documented: **regional endpoints carry a ~1.1× premium over global**; **batch inference
≈50% off**; **prompt caching up to 90% off cached input.**

### PROBE's per-incident cost
PROBE reaches `COMPLETION` with **exactly one row** [F6 — one API call per row]:
```
input  ≈ 2,000 tokens   (5-row shortlist + evidence + 3 runbooks + instructions)
output ≈   600 tokens   (verdict + reasoning + remediation + confidence)
```

| Model | Cost per incident |
|---|---|
| **Sonnet 5** | (2000×$2 + 600×$10)/1e6 = **$0.010** |
| **Haiku 4.5** | (2000×$1 + 600×$5)/1e6 = **$0.005** |

> **The line to say:** *"One cent per incident on Sonnet, half that on Haiku. A thousand
> incidents a month costs ten dollars. And it does not move when your log volume does —
> because the model only ever sees five rows, no matter whether we scanned ten thousand spans
> or ten million. The statistics happen in Elasticsearch; the model gets a shortlist."*

**Why it doesn't grow with volume** — the whole argument in one line: `COMPLETION` bills per
**row**, and PROBE's row count is bounded by the number of *root-cause candidates* (≤5), not
by the number of *spans*. Volume changes the ES|QL scan, not the token bill.

**[REASONING]** Prompt caching is a real further lever: the instruction block and runbook
template are static across incidents, so a large fraction of those 2,000 input tokens is
cacheable at up to 90% off. Mention only if asked — don't over-engineer the answer.

---

## 3. The MTTR claim — restated honestly

❌ **Do not say "20–40 minutes of MTTR"** as though it were a measured fact. It is unsourced
and Bose will call it "an adjective wearing a number's clothes."

✅ **Say instead** — two defensible things:

1. **[DOCUMENTED]** DORA research: **elite-performing teams restore service in under one
   hour; high performers in under a day; medium performers in one day to one week.**
2. **[DOCUMENTED]** The industry consensus in incident-management literature is that
   **diagnosis, not repair, is the bottleneck** — teams spend more time understanding an
   incident than fixing it, and this worsens in microservices where incidents cross service
   and team boundaries and ownership is unclear.

> **The honest framing:** *"I'm not going to quote you an MTTR number for your estate — I
> don't know it. What's well established is that the bottleneck is diagnosis, not repair, and
> that it gets worse the more services you run. PROBE attacks the diagnosis half. And rather
> than assert an improvement, we measured ourselves: here are five faults we injected, and
> here's what we got right."*

**That converts an attackable claim into a demonstrated one.** It is strictly stronger.

⚠ **And the n=5 discipline:** report **"4 of 5 correct, 1 escalated"** — a **count**, never a
percentage. "80% accuracy" from five runs is a statistical claim you cannot defend, and it is
exactly the sentence a sharp judge pulls the thread on.

---

## 4. Market — the honest answer, not a fake TAM

### Who signs the cheque
**The Head of Platform Engineering / SRE** in an organisation running 50+ services that
**already pays for Elastic Observability.** Not a new budget: the **existing observability
line**, specifically the part that would otherwise renew a separate AIOps/incident-response
product.

### The wedge (this is the whole argument)
> **"It runs on the Elastic you already pay for."**

No new vendor. No second telemetry pipeline. No data leaving the cluster. Deployment is a
handful of indices, some Agent Builder tools and a Workflow — not a migration. **[REASONING]**
That matters disproportionately in a cost-sensitive market: the alternative is adding a
per-host or per-GB-priced AIOps product on top of telemetry you already store and pay for.

### Why now
The primitives only recently became available: `CHANGE_POINT` with `BY` grouping is GA in
**9.5** [F1], Agent Builder in **9.2**, Workflows in **9.3** (preview). This design was not
buildable on the Elastic stack eighteen months ago.

### Unit economics
$0.01/incident, **flat in log volume**. Priced per seat or per incident, **margin does not
invert at scale** — the failure mode Virk penalises. The customer's *own* cost curve also
flattens, which is the part that sells.

### ⚠ The honest limitation — say it before he does
**This is a wedge product for the Elastic installed base, not a platform play.** The realistic
outcomes are:
1. An **open-source accelerator** that drives Elastic consumption — which is precisely what
   Elastic wants, and a legitimate services/support business;
2. A **product for shops that have Elastic but not Datadog**, sold as "RCA without a second vendor."

> **To Virk, directly:** *"I'm not going to tell you this is a platform. It's a wedge into the
> Elastic Observability installed base, and the buyer is the SRE lead who's deciding whether
> to renew a separate AIOps product. If I'm honest, the most likely good outcome is that this
> becomes the reference implementation that makes that renewal unnecessary — and that's a
> business Elastic and its partners already know how to sell."*

**[REASONING]** Conceding the ceiling is what buys credibility for the rest. A GTM lead
distrusts a fabricated TAM far more than a modest, well-reasoned wedge.

---

## 5. "What if Elastic ships this next quarter?"

**The answer, in order:**

1. **Elastic ships primitives. PROBE is the loop.** `CHANGE_POINT`, `LOOKUP JOIN`, Agent
   Builder — these already exist and PROBE is *built from* them. What Elastic has not shipped
   is the **closed loop**: detect → name with evidence → retrieve remediation → gate on a
   human → record the outcome → **feed that outcome back into retrieval.**
2. **The compounding asset is the customer's, not the vendor's.** Every approved and rejected
   remediation writes back what actually worked *on this estate*. A vendor can ship better
   retrieval; a vendor cannot ship your incident history.
3. **The grading harness is structurally awkward for a vendor.** It works because *we* caused
   the fault. On production data nobody has the label. And a vendor has little incentive to
   ship a harness whose output is a number showing their own product being wrong.

❌ **Do not lead with #3.** It sounds like a debating point. Lead with #1 — it's the one a
technical panel finds obviously true.

---

## 6. The clear ask (Gate 1 flagged this as missing)

> "What I want from this room: a **design partner** running Elastic Observability with more
> than fifty services, who'll let us point PROBE at a **staging** estate and run the same
> injected-fault harness against **their** topology. The grading harness is the product —
> it'll tell them, and us, in an afternoon whether this works on a system we didn't design."

**[REASONING]** Asking for a design partner rather than a prize signals product intent and
gives Virk something concrete to react to. It also implicitly repeats the strongest claim —
that PROBE can be *measured*.
