# PROBE — Offline Simulation Results
### The algorithm, proven end-to-end without a cluster

**Run it:** `cd research/sim && python3 harness.py && python3 hard_mode.py`

---

## ⚠ What this DOES and DOES NOT prove — read before quoting any number

| ✅ Proves | ❌ Does NOT prove |
|---|---|
| The **sink rule** correctly names the root across every fault in the flagd catalogue | That the **ES\|QL syntax** is valid — that still needs a cluster |
| **Incident splitting** returns N incidents for N unrelated faults | That Elasticsearch's real `CHANGE_POINT` behaves like the stand-in detector |
| The **escalation rule** fires on genuinely ambiguous cases and not otherwise | Real ingest lag, cluster performance, or licence/version availability |
| **Detection needs ~8 post-change buckets**, so bucket width is the latency dial | That real telemetry has this noise profile |

**The detector here is a Welch t-test stand-in, not a reimplementation of
`CHANGE_POINT`.** It only detects step changes — no `distribution_change` or
`trend_change` — so it is **strictly weaker** than the real command. Results are a
**lower bound on capability**, and an **unvalidated estimate on latency**.

> **On stage, say:** *"We simulated the algorithm offline across 70 runs before we
> ever touched a cluster. That's how we found two bugs. The numbers you're seeing
> now are from the real thing."* **Never present simulated numbers as live results.**

---

## Result 1 — Single faults: 70/70 correct

14 faults × 5 seeds. Mirrors `research/artifacts/ground-truth.ndjson`.

```
correct 70/70   escalated 0/70   WRONG 0/70
precision when committed: 70/70 = 100.0%
```

Every flag in the catalogue — errors and latency, 1-hop and 3-hop, sync and async —
resolved to the correct root.

⚠ **100% is a warning sign, not a trophy.** It means the synthetic data is cleaner
than production. It is evidence the **logic** is sound, **not** that PROBE is
accurate. This is exactly why `hard_mode.py` exists.

## Result 2 — Concurrent unrelated incidents: correct

| Scenario | Expected roots | PROBE returned | |
|---|---|---|---|
| payment + recommendation + kafka | `kafka, payment, recommendation` | `kafka, payment, recommendation` | ✅ |
| ad + shipping | `ad, shipping` | `ad, shipping` | ✅ |

9 of 18 services anomalous, split into **3 correctly-scoped incidents**, each with its
own blast radius. `frontend` appears in all three — the **shared victim** the design
predicted.

## Result 3 — Where PROBE actually breaks (`hard_mode.py`)

| Case | Outcome | Assessment |
|---|---|---|
| cart 5% errors | 5/5 correct | weak signal still resolved |
| **cart 2% errors** | **5/5 escalated** | ✅ **correct behaviour** — knows it doesn't know |
| **ad + shipping in the SAME bucket** | **5/5 escalated** | ✅ margin < 1 bucket ⇒ unresolvable. **This is the demo's honest failure case, and it is real.** |
| **fault with only 6 post-change buckets** | **4/5 NO DETECTION** | ⚠ **real limitation** — see Result 5 |
| payment fault + unrelated currency flap | named `currency` + `payment` | ✅ **the test was wrong, not PROBE** — a flap *is* a second incident, and PROBE split it |
| two roots, same subtree (payment + cart) | named both | ✅ two genuine independent faults |
| slow burn (gradual latency) | 5/5 correct | caught, though real `CHANGE_POINT` may label it `trend_change` |

## Result 4 — 🔴 DESIGN BUG FOUND AND FIXED

**The escalation rule killed the flagship demo.**

Original rule (`03-MECHANISM-DESIGN.md` §5): *escalate if >40% of services are anomalous
— probably infra-wide.*

**What the simulation showed:** three genuine concurrent incidents made 9 of 18 services
(50%) anomalous, so the rule fired and **escalated all three incidents at confidence
0.45** — instead of confidently reporting them. The rule triggered on **precisely the
case PROBE exists to handle.**

**Fix:** headcount does not indicate a systemic event. **Unexplained** anomaly does.
If the identified roots' blast radii **cover** the anomalous set, the split is
trustworthy no matter how many services are involved:

```
coverage = |anomalies explained by some root| / |anomalies|
escalate if coverage < 0.80          → something is broken that no root explains
      or if #roots > 5               → implausible as independent incidents
```
**After the fix:** 3 incidents at confidence 0.92, committed. ✅

## Result 5 — 🟠 RECOGNITION LATENCY: I was ~2× optimistic

`07-ARCHITECTURE-SPEED.md` originally estimated **3–6 post-change buckets (20–60 s)**.
**Measured: 8–9 buckets.**

```
bucket   buckets needed   latency   total buckets in 20 min
    5s                8       40s       240
   10s                8       80s       120
   15s                8      120s        80
   20s                8      160s        60
```

**The finding that matters: the detector needs ~8 post-change buckets *regardless of
bucket width*.** So **bucket width is the latency dial**, linearly.

### Consequence — change the demo config
**Use 5-second buckets, not 10.** Recognition drops from ~80 s to **~40 s**, and 240
buckets over 20 min stays comfortably above the 22-value floor [F1].

⚠ **Tradeoff:** smaller buckets mean fewer calls per bucket, so low-traffic services
(`email`, `accounting`) risk falling under the `calls >= 5` filter. **Verify the bucket
count per service at pre-flight step 6 after switching to 5 s.**

> **Corrected claim for stage:** *"About forty seconds from fault to named cause, and I
> can tell you exactly what sets it: the detector needs roughly eight buckets of
> evidence after the change. Bucket width is the dial — halve it and I halve the
> latency and add noise. That's the whole tradeoff."*

**[UNTESTED]** with the real `CHANGE_POINT`, which may need more or fewer points.
**Measure it at pre-flight and quote the real number.**

---

## Summary of what simulation caught that review did not

| # | Found | Severity |
|---|---|---|
| 1 | Escalation rule escalated all 3 incidents in the concurrent demo | 🔴 would have failed live |
| 2 | Recognition latency ~2× worse than estimated | 🟠 over-claim on stage |
| 3 | 5 s buckets halve latency at no floor risk | 🟢 free improvement |
| 4 | Faults near the window edge are undetectable | 🟠 real limitation to disclose |
| 5 | All change points land in one bucket for sync faults | ✅ empirically confirms Gate 2 |

*(Earlier, static analysis caught the inverted messaging edge — Gate 2, Finding 3.)*

**Three bugs, found before the venue.** That is the argument for building the harness
first: PROBE's self-grading is not just the pitch, it is the engineering control.
