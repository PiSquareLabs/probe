# PROBE — Project Overview & Test Evidence (for review)

One page for a judge: what this project is, what it does end to end, and the
proof that each stage behaves the way its design says it does — with the
actual test output, not a claim.

---

## 1. What PROBE is

PROBE is an autonomous incident-response pipeline for a microservices stack
instrumented with OpenTelemetry: **it notices something is wrong, checks
whether it has seen this exact problem before, and if not, reasons about the
root cause and hands a human a filed ticket with the answer.** The goal is to
turn "an SRE stares at dashboards for twenty minutes" into "a ticket appears
with the right root cause already on it."

It's built as **six independent stages**, each a separate module with a
fixed contract for what it takes in and what it hands the next stage. No
stage reaches into another's internals — the harness (a loop, or eventually
a Kibana Agent Builder workflow) passes plain objects between them:

```
Detector → Gate → Remediator → Correlator → Grader → Writer
```

| Stage | Question it answers | Uses an LLM? |
|---|---|---|
| **Detector** | What changed, where, when, how sure? | No — pure statistics (z-score + change-point detection) over Elasticsearch telemetry |
| **Gate** | Should anyone care? | No — deterministic rules over signal type + persistence |
| **Remediator** | Have we seen this before? | Yes, one small "does this explain it?" confirm call — only after a fixed-query search already found a candidate |
| **Correlator** | Why did it happen? | Yes, one reasoning call — only runs when memory has no answer |
| **Grader** | Was the answer right? | **Never.** Ground truth comes only from the fault-injection catalog, never a model, so the score can't be gamed |
| **Writer** | What do we keep? | No — persists the verified answer as a reusable "runbook," or logs a wrong guess so it isn't repeated |

The organizing idea: **cheap and deterministic before expensive and
probabilistic.** An exact repeat of a known fault is a 10ms keyword filter
plus one confirm call (~2-4s total). A genuinely new fault is the only case
that pays for full LLM reasoning (~15-20s). The system gets *faster* over
time as its memory of verified runbooks grows, not slower.

### The two halves of the repo

- **`probe-pipeline/`** — the six-stage pipeline above: Detector output in,
  a graded, memory-backed diagnosis and (optionally) a Kibana Agent Builder
  tool out. This is the analytical core and the part under active
  development on this branch.
- **`backend/` + `frontend/`** — the **Remediator's delivery mechanism**:
  a FastAPI + React app that turns a diagnosis into an actual Jira ticket,
  working around the fact that Elasticsearch's own Jira "action connector"
  requires a Gold licence. Probe detects which licence tier is available at
  runtime and files the ticket itself when it has to, rather than silently
  failing on the free tier.
- **Detector implementations** (`probe-detector/`, `causal-changepoint-detection/`,
  `ml-flag-detection/`, `elastic-ml-anomaly-detection/`, `probe-two-tier-detector*/`)
  — five independently-built and independently-validated methods for the
  "what changed" question, benchmarked against each other on the same
  11-fault battery (see [`DETECTORS.md`](DETECTORS.md) for the honest,
  including-the-losses comparison table).

### Why this design is defensible, not just convenient

- **The Grader never sees a model, ever.** Ground truth is a hand-written
  `(fault_class, service)` pair per injected fault in `catalog/*.yaml`. No
  stage upstream of the Grader can read it. This is the one rule that makes
  every accuracy number in this project real instead of self-graded.
- **Retrieval is fixed queries, not agent-written search.** The Remediator's
  memory lookup runs the same three parameterized ES|QL queries every time
  — the LLM only judges the *result*, never decides *how to search*. This
  is deliberate (see [`PROBE-runbook-search-cases.md`](PROBE-runbook-search-cases.md)
  §5): an agent-written query makes the hit rate a property of the model's
  mood that day instead of a property of the memory.
- **A wrong answer is never silently reused.** When a memory hit turns out
  to be wrong (graded against truth), the runbook is demoted and a
  "ruled out" record is written for that symptom — the *true* answer is
  never written on a miss, so the system can't accidentally leak the
  answer key into its own memory.

---

## 2. Full structure

```
probe/
├── backend/, frontend/, docker-compose.yml   # Remediator delivery: Jira ticket UI + API
├── opentelemetry-demo/                       # submodule: the "Astronomy Shop" demo app, fault-injectable via flagd
├── catalog/                                  # ground truth: one YAML per injectable fault → (fault_class, service)
├── probe-pipeline/                           # <-- the six-stage pipeline (this doc's main subject)
│   ├── schemas.py            # Candidate, Fingerprint, Decision, Diagnosis, RemediatorOutput — the shared objects
│   ├── gate.py                # Stage 2: incident / transient / watch decision + persistence counters
│   ├── remediator.py          # Stage 3: cache → stage0 exact → stage1 hybrid → confirm (LLM)
│   ├── correlator.py          # Stage 4: graph rank, deepest-span, K-S correlation, one reasoning call (LLM)
│   ├── grader.py              # Stage 5: correct_at1/at3, abstention, no LLM, no ES
│   ├── writer.py              # Stage 6: upsert/demote runbooks, ruled-out records, cache invalidation
│   ├── detector_bridge.py     # bridges the standalone probe-two-tier-detector-v3 Detector into this pipeline
│   ├── es_client.py           # thin Elasticsearch client (ESQL, get/index/update doc)
│   ├── llm_openai.py / llm_bedrock.py   # the two pluggable LLM backends for confirm()/reason()
│   ├── agent_builder.py       # provisions/updates Kibana Agent Builder tools from a verified runbook
│   ├── catalog_runbook.py     # reads catalog/*.yaml ground truth for grading
│   ├── smoke_test.py          # <-- 24 deterministic checks, no live ES/LLM needed (§3 below)
│   ├── test_agent_builder.py  # <-- 23 deterministic checks against a faked Kibana (§3 below)
│   └── validate_pipeline.py   # live-stack test: inject a real flagd fault, run the whole pipeline, grade it
├── probe-detector/, probe-two-tier-detector{,-v2,-v3}/   # standalone Detector implementations + their own batteries
├── causal-changepoint-detection/, ml-flag-detection/, elastic-ml-anomaly-detection/  # alternative Detector methods
└── docs/, DETECTORS.md, PROBE-*.md   # design docs, contracts, and the comparison table across all Detector methods
```

---

## 3. Success test cases — actual output, run just now

Two suites run against `probe-pipeline/` with **no live Elasticsearch, no
Docker, and no API key** — every external call is faked or stubbed, so
these check "does the code do what its own spec says," not "does it work
against a real cluster" (that's `validate_pipeline.py`, which needs the
live demo stack — see §4). Both were re-run for this document:

```bash
cd probe-pipeline
python smoke_test.py
python test_agent_builder.py
```

### 3a. `smoke_test.py` — pipeline contract checks (24/24 passed)

Covers Gate, Remediator (cases A/B/C/J from
[`PROBE-runbook-search-cases.md`](PROBE-runbook-search-cases.md)), Correlator,
Grader, and Writer.

```
[ok] Gate: tier-2 non-recovering symptom -> incident/sustained
[ok] Gate: third spike on same (service,signal) in 10min -> incident/intermittent
[ok] Gate: resource-only signal never opens an incident alone
[ok] Gate: same-scan resource candidate rides along as supporting_evidence
[ok] Remediator Case A: empty memory -> memory_miss, 0 candidates
[ok] Remediator Case B: stage-0 exact hit + confirm -> memory_hit
[ok] Remediator Case C: cache hit skips stage 0/1 entirely
[ok] Remediator Case J: malformed confirm response -> memory_miss, never a hit
[ok] Remediator: raises on non-incident Decision
[ok] Correlator: runs on memory_miss, all 8 evidence keys present
[ok] Correlator: unwired reasoning_fn fails closed to unknown
[ok] Correlator: raises on memory_hit input
[ok] Grader: fault_class+service both match -> correct_at1
[ok] Grader: mismatch on either field -> not correct_at1
[ok] Grader: null window, Gate said watch -> correct
[ok] Grader: null window, Gate opened an incident -> false positive
[ok] Writer: correct grade creates a runbook
[ok] Writer: cache pre-condition set correctly
[ok] Writer: correct grade again -> occurrences++, symptom appended, semantic widened
[ok] Writer: that write called remediator.invalidate(), clearing the cache
[ok] Writer: wrong + memory_hit -> demotes the reused runbook
[ok] Writer: the TRUE answer is never written anywhere
[ok] Writer: abstained -> writes nothing
[ok] Writer: ablation run -> never touches memory

ALL 24 CHECKS PASSED
```

**Why these particular checks matter to a judge**, in order:

1. **Gate correctly separates "someone should look at this" from "system
   noise."** A resource metric (`cpu`) spiking alone never opens a ticket —
   only a symptom (`p95_latency`, `error_rate`) does, optionally with the
   resource metric riding along as supporting evidence. This is the
   difference between a useful alert and pager fatigue.
2. **The Remediator's cache genuinely skips the search, not the check** —
   Case C proves `_stage0_exact` was called zero times on a cache hit, while
   still running the LLM confirm step every time (so a demoted runbook can
   never be silently reused).
3. **A malformed LLM response is treated as "no," never as "yes."**
   (Case J) — an unparseable confirm call fails closed to `memory_miss`,
   not to a false hit.
4. **The Correlator refuses to run on a memory hit and refuses to be run
   without a valid `Decision`** — these are `ValueError`s the test actually
   triggers and catches, not documentation claims.
5. **The Writer never writes the true answer on a wrong guess** — checked
   by scanning the entire fake datastore for the string `resource_exhaustion`
   after a graded-wrong run and asserting it appears nowhere. This is the
   guarantee that keeps future grading honest.
6. **Ablation runs (`config != FULL`) touch memory zero times** — required
   so that "Remediator off" / "Correlator off" experiments produce a clean
   before/after comparison instead of a smeared one.

### 3b. `test_agent_builder.py` — Kibana Agent Builder provisioning (23/23 passed)

Fakes `urllib.request.urlopen` itself (not `agent_builder`'s own functions),
so the real HTTP-building code (`_request()`, `_auth_header()`) runs for
real against a scripted fake Kibana.

```
auth falls back to Basic when KIBANA_API_KEY unset: ok
KIBANA_API_KEY takes priority when set: ok
get_tool returns None on 404: ok
first upsert reports created: ok
exactly one POST was made: ok
created tool id matches Writer._runbook_id convention: ok
created tool type is index_search, not esql: ok
created tool pattern is the OTel telemetry glob: ok
description names the service: ok
description carries the verified root cause: ok
description carries the verified steps: ok
description carries the signature: ok
tags include the service for discoverability: ok
POST carries the kbn-xsrf header a real Kibana requires: ok
get_tool finds the tool after creation: ok
second upsert on the same key reports updated, not created: ok
exactly one PUT was made: ok
no second POST happened (idempotent, not a duplicate-id error): ok
PUT omits the immutable id field: ok
PUT omits the immutable type field: ok
PUT's description reflects the revised root cause: ok
dead Kibana connection reports provisioned=False instead of raising: ok
failure result still names the tool id that would have been created: ok

ALL 23 CHECKS PASSED
```

**Why these matter:** this is the step that turns a *verified* runbook into
a live, chat-queryable tool inside Kibana. The tests prove it's idempotent
(a second confirmation on the same fault updates instead of erroring or
duplicating), that it degrades gracefully when Kibana is unreachable
(returns `provisioned: False` instead of crashing the Writer), and that the
auth header correctly prefers an explicit `KIBANA_API_KEY` over falling back
to basic auth.

**Total: 47/47 deterministic checks passing**, covering every module in the
pipeline except the LLM backends themselves (which are pluggable and
intentionally excluded — see `llm_openai.py`/`llm_bedrock.py`).

---

## 4. What's *not* covered by the deterministic suite (and where it's tested instead)

- **The Detector's real recall/precision against live faults** — this is a
  statistics problem, not a contract problem, and is validated separately
  per implementation against the real OpenTelemetry demo with fault
  injection. See [`DETECTORS.md`](DETECTORS.md) §3 for the full 11-fault
  comparison table across five independent methods (best: 5/11 detected at
  a mean 38.3s, or up to 5/8 confirmed on the in-scope subset for the
  latest two-tier detector).
- **The full pipeline wired to a live cluster and a live LLM** —
  `probe-pipeline/validate_pipeline.py` injects a real flagd fault into the
  running demo, polls the real Detector until Gate opens an incident, runs
  it through Remediator → Correlator → Grader → Writer, and writes a
  `pipeline_validation_results.json` with per-flag timing and
  correct/wrong/abstained outcomes. This needs `ES_URL`/`ES_API_KEY` and,
  for a real (non-stubbed) answer, `--use-openai` with `OPENAI_API_KEY` set.
- **The Jira ticket-filing app** — has its own 58-test `pytest` suite under
  `backend/` (licence-gate routing, both connector paths, ADF conversion,
  JQL escaping, encryption at rest) — run with `cd backend && pytest -q`.

---

## 5. One-line answers for likely judge questions

- *"Does this actually run, or is it just diagrams?"* — 47/47 deterministic
  checks pass right now, reproducible with `python smoke_test.py` and
  `python test_agent_builder.py` in `probe-pipeline/`, no cloud account
  needed.
- *"How do you know the accuracy numbers aren't self-graded?"* — The Grader
  is the only module allowed to read the fault-injection catalog
  (`catalog/*.yaml`); no LLM call happens inside it, ever.
- *"What happens when the memory is wrong?"* — It gets demoted and logged
  as ruled-out, and the true answer is never written back — proven by
  `smoke_test.py`'s "the TRUE answer is never written anywhere" check.
- *"Why not let the agent decide how to search memory?"* — Because then the
  hit rate becomes a property of the model's mood, not the memory. See
  [`PROBE-runbook-search-cases.md`](PROBE-runbook-search-cases.md) §5 for
  the full reasoning, with a worked example (Case E) of the system
  correctly saying "no, this isn't the same fault."
