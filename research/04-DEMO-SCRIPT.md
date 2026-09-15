# PROBE — Minute-by-minute live demo script

**Design principle:** every number on screen is computed live from data created
in the room. The only pre-staged things are the ones that cannot fail
gracefully (index templates, the service graph, the runbook corpus) — and I say
so out loud, because admitting what's pre-baked buys credibility for everything
that isn't.

**Total: 8 minutes.** Assumes the app has been running ~20 minutes so every
series has ≥22 buckets before we touch anything. **[F1: CHANGE_POINT needs ≥22.]**

---

## T−15 min (before we're called up) — the silent prerequisite
Astronomy Shop running, load generator on, all flags `off`. This builds the
**baseline** the change-point detector needs. **If the app has been up <4 minutes,
CHANGE_POINT returns nothing and the demo is dead.** Non-negotiable.

---

## 0:00 – 0:45 — The problem, with a live screen (not a slide)
Kibana open on the running system. All green.

> "This is the OpenTelemetry Astronomy Shop — 15 services, real traces going
> into Elasticsearch. I'm going to break it, and I'm not going to tell PROBE
> how. But I *will* tell you, so you can grade it with me."

Show `probe-ground-truth` — empty. "That's the scoreboard. It's empty because
nothing's happened yet."

---

## 0:45 – 1:15 — Inject fault #1, on camera
Flip **`productCatalogFailure` → `on`** in the flagd UI.

Write ground truth: `flag_name: productCatalogFailure`,
`expected_root_svc: product-catalog`. **Say the answer out loud now.**

> "The answer is product-catalog. Remember that. PROBE doesn't know it."

**Why this fault:** `product-catalog` is called by `frontend`, `recommendation`
**and** `checkout`, so ≥4 services go anomalous and only one is the cause. It is
the cleanest possible demonstration that "many symptoms, one cause" is real.

---

## 1:15 – 2:15 — The baseline fails, in one query
Run `05-baseline-vs-probe-fork.esql` (fallback: `05b` + `01` side by side).

**One query. Two branches. Same input rows** — `FORK` guarantees that. **[F8]**

- `fork1` (threshold): fires on `frontend`, `recommendation`, `checkout`,
  `product-catalog` — **4 alerts, no cause.**
- `fork2` (change point): same 4 services, but each with **the moment it
  changed**.

> "Same data, same query, two branches. The threshold gives me four things to
> look at. The change points give me four things *in an order*. Order is what
> makes the next step possible."

**This is the Elasticsearch-depth moment.** Say the words `FORK`, `_fork`
discriminator, `CHANGE_POINT`, p-value.

---

## 2:15 – 3:15 — The causal step (the win)
Run `03-causal-rank.esql`.

Talk through it in beginner-safe language while it runs:
> "Two joins. The first asks, for each broken service, *what does it depend on?*
> The second asks, *are any of those also broken?* If a service depends on
> nothing that's broken, nothing upstream explains it — so it's a candidate.
> Then I sort by who changed first. That's the whole rule."

**Output: `product-catalog`, `verdict: root_cause_candidate`**, with
`anomalous_deps: 0`, its change-point time, its p-value, and `frontend`,
`recommendation`, `checkout` classified as `downstream_victim`.

> "No model has been called yet. That's a query."

---

## 3:15 – 4:00 — Bedrock confirms, and is allowed to disagree
Agent Builder run. The agent gets the five-row shortlist, retrieves the runbook
via ELSER+BM25, returns verdict + confidence + proposed remediation.

> "The model didn't find the root cause — the query did. The model's job is to
> confirm it against the evidence, pull the runbook, and write the remediation a
> human can read. It sees five rows, never five million. That's why our cost per
> incident doesn't move when your log volume does."

Show the Kibana Case: **`awaiting_approval`**.
> "It has not done anything. It can't. It has no write access."

---

## 4:00 – 4:30 — ⭐ THE GROUND-TRUTH MOMENT
Run `06-self-grade.esql`. On screen:

```
incidents: 1 | correct: 1 | accuracy_pct: 100.0 | mean_ttd_s: <live>
```

> "We flipped the flag. We knew the answer before it did. That's the number."

---

## 4:30 – 6:00 — ⭐⭐ THE CONCURRENT CASE (the originality anchor)
Reset flags. Now flip **three at once**:
- `paymentFailure → 100%`  (root: `payment`)
- `recommendationCacheFailure → on` (root: `recommendation`)
- `kafkaQueueProblems → on` (root: `kafka`, async path)

Chosen because their dependency subtrees are **disjoint except at the hub**.

Run `05b` first — the baseline. It fires on 7–9 services. One undifferentiated wall.
> "That's an alert storm. Every tool in this category sells you *compression* —
> collapse that into one incident. Watch what that does when the faults are
> unrelated: you get one incident with three causes and you page the wrong team."

Run `04-split-concurrent-incidents.esql`. **Three rows.**

| root_svc | blast_size | blast_radius |
|---|---|---|
| payment | 3 | payment, checkout, frontend |
| recommendation | 2 | recommendation, frontend |
| kafka | 3 | kafka, accounting, fraud-detection |

> "Three incidents, three causes, three teams. And notice `frontend` appears
> twice — because it genuinely is a victim of both. We don't force the incidents
> to be disjoint in *services*; we make them disjoint in *cause*. If you'd run
> connected components here you'd get one blob, because frontend touches
> everything. That's the bug we designed around."

Self-grade again: **3 injected, 3 correct.**

---

## 6:00 – 7:00 — ⭐⭐⭐ THE HONEST FAILURE CASE
Flip **`adHighCpu → on`** *and* **`adFailure → on`** together — two faults in the
**same** service, plus **`cartFailure → 10%`**, a weak intermittent signal.

PROBE returns:
```
incident 2: predicted_root = cart | confidence 0.58 | ESCALATED
reason: margin < 1 bucket — cart and checkout changed in the same 10s window;
        temporal precedence is not resolvable at this resolution.
competing hypotheses: cart, checkout
```
Case filed `awaiting_human`. **No action taken. Self-grade counts this as
`escalated`, NOT as correct — even though `cart` was right.**

> "I want you to see this one. At 10% failure the signal is genuinely weak, and
> two services changed inside the same ten-second bucket. PROBE can't order them,
> so it doesn't pretend to. It says 58%, hands you both hypotheses and the
> evidence, and stops. And our own scoreboard refuses to give us the point —
> even though the guess was right. A system that's never unsure is a system
> that's lying to you."

**[REASONING]** This single case moves Demo Quality, AI Implementation and
Problem Solving simultaneously, and it is the most common thing a panel finds
missing.

---

## 7:00 – 8:00 — Close
```
Injected: 5   Correct: 4   Escalated: 1   Wrong: 0
Mean time to root cause: <live> seconds
Baseline: fired on 12 symptom services, named 0 causes.
```
> "Four right, one honestly escalated, nothing wrong, and it told you which was
> which. Root-cause analysis isn't new — Datadog and Dynatrace both do causal
> RCA. What's new is that you just watched a system be graded on it, on data it
> had never seen, by a truth we set before it answered."

---

## Fallbacks (rehearse each)
| Failure | Fallback | Cost |
|---|---|---|
| `FORK` rejects `CHANGE_POINT` | `05b` + `01` side by side | 15 s, elegance only |
| `CHANGE_POINT` errors (licence) | **Blocker** — trial must be active. Verify T−60. | fatal |
| `BY` unsupported (<9.5) | Loop `01` per service via the agent | 30 s |
| `LOOKUP JOIN` fails after `STATS` | Pre-materialise anomalies, join in a 2nd query | 45 s |
| Bedrock timeout | Pre-recorded agent output shown as *"here's what it returns"* — **say it's recorded** | credibility if hidden |
| Load generator died | Restart + wait 4 min — **check at T−5** | fatal if unnoticed |
| Change point not found | Widen window to 30 min, re-run | 20 s |
