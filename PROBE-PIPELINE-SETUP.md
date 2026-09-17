# PROBE Pipeline — Setup for a Fresh Machine

`probe-pipeline/` (Gate, Remediator, Correlator, Grader, Writer) and
`probe-two-tier-detector-v3/` (the Detector + `change_point()`) were
built and tested by hand-wiring them together with mocked Elasticsearch
calls — they currently only run for real on the machine that already has
Elasticsearch/Kibana credentials and the demo stack configured. This
document is everything a *different* machine (or a coding agent working
on one) needs to actually run them, in the order it needs it.

If you're an agent reading this cold: read `PROBE-component-contracts.md`
and `PROBE-runbook-search-cases.md` first — they're the spec both
`probe-pipeline/` and this setup guide implement. This document is
*only* "how do I make the machine able to run that code," not "what does
the code do."

---

## 1. What has to exist before any of this runs

### 1a. The demo stack + Elasticsearch

`probe-two-tier-detector-v3/` reads real telemetry (`traces-*`, `logs-*`,
`metrics-*`) — it has nothing to scan without a running OpenTelemetry
demo app exporting into a real Elasticsearch. Follow one of:

- **[RUNNING_LOCALLY.md](RUNNING_LOCALLY.md)** — self-hosted Elasticsearch
  via `elastic-start-local`, everything on one machine.
- **[RUNNING_ON_ELASTIC_CLOUD.md](RUNNING_ON_ELASTIC_CLOUD.md)** — demo app
  still runs locally in Docker, but exports to a real Elastic Cloud/
  Serverless deployment instead.

Either way, confirm telemetry is actually arriving before touching
anything in this document — `RUNNING_ON_ELASTIC_CLOUD.md` §2d's
`FROM traces-*.otel-default | LIMIT 5` check (or the equivalent local
one) returning rows is the gate. Nothing downstream will work on an
empty cluster, and the failure mode is confusing (empty candidate lists,
not a clear error).

### 1b. The three environment variables everything reads

Every ES client in this repo — `probe-two-tier-detector-v3/es_client.py`,
`probe-two-tier-detector-v3/change_point.py`, and every `es_client.py`
under `probe-pipeline/` and `catalog/` — checks the same three variables,
falling back to a local `elastic-start-local/.env` password if none are
set:

| Variable | Purpose |
|---|---|
| `ES_URL` | Elasticsearch endpoint. Defaults to `http://localhost:9200`. |
| `ES_API_KEY` | Preferred for Cloud/Serverless. |
| `ES_USERNAME` / `ES_PASSWORD` | Basic-auth alternative. |

```bash
export ES_URL="https://<your-deployment>.es.<region>...elastic.cloud"
export ES_API_KEY="<api key>"
```

**Set these before importing `probe-pipeline/correlator.py`, not just
before calling it.** `probe-two-tier-detector-v3/change_point.py`
resolves its auth header at *module import time*
(`_AUTH_HEADER = _auth_header()`, not inside a function), and
`correlator.py` imports `detector_bridge.py`, which imports
`change_point.py`, by default. If `ES_URL`/`ES_API_KEY` aren't set yet,
`import correlator` itself raises `RuntimeError`/`FileNotFoundError`
before you ever get to call anything.

### 1c. Python packages

```bash
pip install pyyaml
```

That's the only non-stdlib dependency anywhere in `probe-pipeline/` or
`probe-two-tier-detector-v3/` — everything else (`urllib`, `json`,
`dataclasses`) is stdlib on purpose, matching this repo's existing
memory-conscious convention (see `ml-flag-detection/README.md` for why).
`pyyaml` is only needed for `probe-pipeline/catalog_runbook.py`; skip it
if you're not using that module.

### 1d. OpenAI, only if you want the real confirm/reasoning calls

`probe-pipeline/llm_openai.py` implements the Remediator's stage-2
confirm and the Correlator's reasoning call using `gpt-4o-mini`. It's
**not** wired in as either module's default — both default to safe stubs
that always answer "no match" / `unknown`, so nothing in §2 below needs
this. If you want real LLM calls:

```bash
export OPENAI_API_KEY="<your key>"
```

and construct the classes with it explicitly (§3).

---

## 2. Verifying the Detector works standalone, before touching probe-pipeline

Do this first, in isolation, before wiring anything else — it's the
easiest layer to debug because it has no other module's state to worry
about.

```bash
cd probe-two-tier-detector-v3
python detector.py --verbose
```

Expected output: either `"No candidates. System looks stable."` or a
list of `tier=1`/`tier=2` candidates. If this fails, the problem is in
§1 (telemetry not arriving, or `ES_URL`/`ES_API_KEY` wrong) — don't move
on to `probe-pipeline/` until this works.

To test `change_point()` in isolation on one service/signal:

```bash
python change_point.py cart p95_latency --lookback 5 --bucket 2
```

`--loop 10` runs `detector.py` continuously, carrying `streak_state`
across iterations — needed for Tier 1's persistence (`PERSISTENCE = 2`
consecutive scans) to mean anything at all. A single `python
detector.py` invocation starts `streak_state` empty every time, so
persistence effectively requires two *manual* re-runs to ever fire; see
the module's own docstring.

---

## 3. Verifying probe-pipeline imports and runs

```bash
cd probe-pipeline
python -c "import gate, remediator, correlator, grader, writer, detector_bridge, github_runbook, catalog_runbook"
```

If this fails on `correlator`/`detector_bridge` with a credentials error,
go back to §1b — this is the eager-import-time auth resolution again.

### 3a. The `probe-memory` index has to exist

`remediator.py`'s three ES|QL tools and `writer.py`'s upsert/ruled-out
writes all assume an index called `probe-memory` with a specific
mapping. Nothing creates it automatically:

```bash
python setup_probe_memory.py
```

Idempotent — safe to re-run. Creates the index (and the `.elser-2-elasticsearch`
inference endpoint it uses for the `semantic` field, if that endpoint
doesn't already exist) only if `probe-memory` doesn't already exist.

### 3b. One real scan through Gate → Remediator → Correlator

There is **no harness/orchestrator yet** — nothing loops
Detector → Gate → Remediator → Correlator → Grader → Writer
automatically. Wire one scan by hand to confirm the whole chain works
end to end:

```python
import sys; sys.path.insert(0, ".")
import detector_bridge, gate, remediator, correlator

streak_state = {}          # own this across scans for Tier 1 persistence
g = gate.Gate()             # owns transient counters across scans

raw = detector_bridge.scan(streak_state)
decision = g.evaluate(raw)
print(decision.kind, decision.pattern)

if decision.kind == "incident":
    from schemas import Fingerprint
    fp = Fingerprint(
        change_point=decision.trigger[0].type,
        metric=decision.trigger[0].signal,
        loudest_service=raw["loudest"],
        dependency=None,   # filled in properly once the Correlator's
                            # deepest-span query has run -- see contract §0
    )
    rem = remediator.Remediator()
    rem_out = rem.remediate(decision, fp, symptom="describe the symptom here")
    print(rem_out.path)

    if rem_out.path == "memory_miss":
        corr = correlator.Correlator()
        diagnosis, evidence = corr.correlate(decision, raw, rem_out, symptom="describe the symptom here")
        print(diagnosis.candidates)
```

`streak_state` and the `Gate()` instance both need to be the **same
object across every scan** in whatever process runs this loop — a fresh
`{}`/`Gate()` every call resets persistence and transient counters to
zero each time, which defeats both Tier 1's `PERSISTENCE=2` and the
Gate's intermittent-pattern detection (third transient in 10 minutes).
This matters for whoever eventually writes the actual harness loop, not
just for this one-off test.

---

## 4. What is genuinely not built yet — don't assume otherwise

- **No harness/orchestrator.** Nothing calls all six stages in a loop
  against live, ongoing telemetry. §3b above is a manual one-shot wiring,
  not a running service.
- **The two LLM calls are stubs by default.** `remediator.py`'s confirm
  and `correlator.py`'s reasoning both fail closed (no-match / `unknown`)
  unless you explicitly pass `llm_openai.confirm` / `llm_openai.reason`
  in (§1d).
- **Grader's baseline answers aren't computed.** `grader.grade_incident()`
  accepts a `baseline_answers` dict as an opaque parameter; nothing
  computes the threshold/loudest/LLM-only/ml-classifier comparison
  numbers the contract describes.
- **No GitHub PR integration.** `writer.py`'s `open_pr_fn` defaults to a
  stub that reports "not wired" — there's no GitHub client/token
  anywhere in this repo. `github_runbook.py`'s *read* side works against
  a local checkout or the Contents API, but nothing writes there for real.
- **`(fault_class, service)` key collisions are real, not hypothetical.**
  `catalog_runbook.py` surfaced that `adHighCpu` and `adManualGc` — two
  genuinely different faults — both key to `(resource_exhaustion, ad)`.
  `writer.py`'s runbook id uses the same key scheme, so this collision
  would happen for real in `probe-memory` too if both ever get graded
  correct. Not fixed; a design decision someone needs to make.

---

## 5. Quick reference — every file and what it needs

| File | Needs | What it is |
|---|---|---|
| `probe-two-tier-detector-v3/detector.py` | §1a, §1b | Tier 1 z-score + Tier 2 change-point Detector |
| `probe-two-tier-detector-v3/change_point.py` | §1a, §1b | `change_point(service, signal)`, importable standalone |
| `probe-pipeline/detector_bridge.py` | §1b (imports the above) | Adapts v3's dict output into `schemas.Candidate` |
| `probe-pipeline/gate.py` | Nothing (no ES, no LLM) | Incident/transient/watch classification |
| `probe-pipeline/remediator.py` | §1b, §3a | Cache → stage 0 → stage 1 → ruled-out → confirm |
| `probe-pipeline/correlator.py` | §1b (via `detector_bridge`) | Evidence-gathering + one reasoning call |
| `probe-pipeline/grader.py` | Nothing (no ES, no LLM) | Scores a Diagnosis against injected truth |
| `probe-pipeline/writer.py` | §1b, §3a | Upserts/demotes runbooks, writes ruled-out records |
| `probe-pipeline/llm_openai.py` | §1d | Optional real `gpt-4o-mini` confirm/reasoning |
| `probe-pipeline/github_runbook.py` | Nothing, or `GITHUB_TOKEN` for the API path | Reads the human-facing Markdown mirror |
| `probe-pipeline/catalog_runbook.py` | `pyyaml` only | Reads `catalog/*.yaml`'s 13 hand-written write-ups |
| `probe-pipeline/setup_probe_memory.py` | §1b | One-time: creates the `probe-memory` index |
