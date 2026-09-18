# PROBE — Live Testing Guide (`watch_pipeline.py`)

Everything learned running `probe-pipeline/watch_pipeline.py` against a real
Elastic Cloud Serverless project and a real, live-injected Astronomy Shop
fault. `PROBE-PIPELINE-SETUP.md` covers getting the code installed and
importable; this document covers actually running it against live data and
the real gotchas that cost real time to find.

If you haven't read `PROBE-component-contracts.md` /
`PROBE-runbook-search-cases.md` / `PROBE-PIPELINE-SETUP.md` yet, read those
first — this document assumes the pipeline is already installed and
importable.

---

## 1. The two watch scripts, and which one you want

| | `validate_pipeline.py` | `watch_pipeline.py` |
|---|---|---|
| Injects the fault itself | Yes (edits `demo.flagd.json`) | **No, ever** |
| Who injects | The script, on its own machine | Someone else, on theirs |
| Filters to one flag | Always (needs to know what it injected) | Optional (`--flag`) |
| Grades/writes | Always (if catalog entry exists) | Only if `--flag` given |

**Use `watch_pipeline.py` whenever fault injection happens on a different
machine than the one running the pipeline** — the common case once you're
past initial setup. It polls the real Detector and reacts to whatever it
finds, without ever touching `demo.flagd.json`.

---

## 2. Required environment, every time

```bash
export ES_URL="https://<your-project>.es.<region>...elastic.cloud"
export ES_API_KEY="<elastic api key>"
export OPENAI_API_KEY="sk-..."          # only if using --use-openai

# Only needed for Elastic Cloud Serverless -- see section 4.
export SPAN_KIND_FIELD="kind"
export SPAN_KIND_SERVER_VALUE="Server"
```

PowerShell: `$env:NAME = "value"` instead of `export NAME=value`.

**Without `--use-openai`**, both LLM calls (Remediator confirm, Correlator
reasoning) are fail-closed stubs — every incident comes back `memory_miss`
and `fault_class="unknown"`, always. That's expected, not a bug; the script
prints a warning about this on startup if you forget the flag.

---

## 3. Before you start watching: verify the fault is *actually* real

**This is the single biggest time-sink if skipped.** Across many attempts,
"the flag is on" and "the flag is producing a detectable effect in the
data" turned out to be different things more often than not — containers
not receiving traffic, a flag toggled on then off again before anyone
checked, or a report that didn't match reality. Always verify directly
before spending a watch window:

```
POST _query
{
  "query": "FROM traces-generic.otel-default | WHERE service.name == \"<service>\" AND @timestamp > NOW() - 5 minutes | STATS count = COUNT(*), errors = COUNT(CASE(status.code == \"Error\", 1, null)) BY minute = DATE_TRUNC(1 minute, @timestamp) | SORT minute DESC"
}
```

For a CPU/memory-based fault (`adHighCpu`, `adManualGc`, `emailMemoryLeak`),
check the metric directly instead:
```
POST _query
{
  "query": "FROM metrics-generic.otel-default | WHERE service.name == \"<service>\" AND @timestamp > NOW() - 5 minutes | EVAL cpu = COALESCE(TO_DOUBLE(metrics.process.cpu.utilization), TO_DOUBLE(metrics.jvm.cpu.recent_utilization)) | WHERE cpu IS NOT NULL | STATS avg_cpu = AVG(cpu) BY bucket = DATE_TRUNC(30 seconds, @timestamp) | SORT bucket DESC"
}
```

A real fault shows a clean before/after step in these numbers. If it looks
flat, don't start watching yet — go find out why first.

---

## 4. Elastic Cloud Serverless: the `SPAN_KIND_FIELD` gotcha

`change_point.py` (Tier 2) filters on a span-kind field to scope queries to
`SERVER` spans. **On local self-hosted Elasticsearch this field is
`span.kind` with value `"SERVER"`** (OTel semconv casing) — that's what the
originally-validated battery ran against. **On this Elastic Cloud
Serverless project, the real field is `kind`, with Title-case values**
(`"Server"`, `"Client"`, `"Internal"`). Confirm which your project uses:

```
POST _query
{
  "query": "FROM traces-generic.otel-default | WHERE service.name == \"<any active service>\" | LIMIT 5 | KEEP kind, span.type, name"
}
```

If `span.kind` errors with `Unknown column`, you're on the Serverless
shape — set the two env vars from section 2. **This is silent if you don't
set it**: `change_point.py`'s error handling swallows any query error other
than "insufficient data" and returns `None` (a bare "no break"), so Tier 2
looks like it's just not finding anything, when it's actually failing on
every single call. If detection looks completely dead despite genuinely
active faults and healthy traffic, check this first.

---

## 5. Running the watch

```bash
cd probe-pipeline
python watch_pipeline.py --flag adFailure --use-openai
```

Every flag:

| Flag | What it does |
|---|---|
| `--flag <name>` | Grades against `catalog/<name>.yaml`, writes to `probe-memory` if correct. Also **filters** incidents to that flag's target service — without it, the first incident on *any* service gets handled, which is fine for observation but can't be graded. |
| `--use-openai` | Real `gpt-4o-mini` confirm/reasoning instead of fail-closed stubs. |
| `--continuous` | Keep watching after handling an incident, instead of exiting after the first. **Use this** — see section 7 on why restarting loses state. |
| `--minutes N` | Give up after N minutes of nothing. Omit to watch until Ctrl+C. |
| `--min-spans-per-bucket N` | Lower Tier 1's per-bucket sample floor (default 20). See section 6. |
| `--min-error-count N` | Lower Tier 1's raw-error floor (default 5). See section 6. |
| `--skip-gate` | **Testing only.** Bypasses `gate.py` entirely — any `tier=2` candidate fires immediately, no persistence check. See section 8 for why this is dangerous to trust. |

---

## 6. The two detection floors that block genuine-but-thin signals

`zscore_scan.py` has two noise-suppression floors, both there for good
reason (stop a near-zero baseline from producing a huge z-score off two
stray events) — but both can also block a *real* fault if traffic is
modest:

- **`MIN_SPANS_PER_BUCKET` (default 20)** — a 10-second bucket needs at
  least this many spans before Tier 1 will even compute a z-score for it.
- **`MIN_ERROR_COUNT` (default 5)** — for `error_rate`/`log_error_count`
  specifically, the most recent 30-second window needs at least this many
  *raw* errors, not just a high rate.

**Concrete example that hit this exactly**: `adFailure` injects ~10%
errors. At `ad`'s actual traffic (~40 requests/minute), a 30-second window
only produces 1-2 raw errors — never enough to clear the default floor of
5, no matter how long you wait. Verified directly:
```
POST _query
{
  "query": "FROM traces-generic.otel-default | WHERE service.name == \"ad\" AND kind == \"Server\" AND @timestamp > NOW() - 3 minutes | EVAL is_error = CASE(status.code == \"Error\", 1, 0) | STATS total = COUNT(*), fail_count = SUM(is_error) BY bucket = DATE_TRUNC(10 seconds, @timestamp) | SORT bucket DESC"
}
```
If no 3-consecutive-bucket window's `fail_count` sum reaches 5, that's your
answer — lower the floor for testing:
```bash
python watch_pipeline.py --flag adFailure --use-openai --min-error-count 2
```

**These are testing overrides, not permanent fixes.** Lowering them makes
Tier 1 more sensitive to real noise too. Don't treat a result produced with
a lowered floor as representative of production behavior at that traffic
level.

---

## 7. Don't restart between checks — you lose accumulated state

Two kinds of state live inside one running Python process and reset to
zero every time you start a new one:

- **`gate.Gate()`'s transient counters** — needed for the `intermittent`
  escalation rule (3 transients on the same `(service, signal)` within 10
  minutes). Restarting the watch to check on something else, then starting
  a new one, throws this away. Concretely: one fault fired `ad/p95_latency`
  as a transient 3 times total across two separate watch launches (1 + 2)
  — the exact threshold — but never escalated, because no single process
  ever saw all 3.
- **Tier 1's `streak_state`** (persistence, 2 consecutive scans) — same
  problem, smaller window.

**Use `--continuous` and one long-running process** rather than stopping
and restarting to run diagnostic checks in between. If you must check
something else, do it in a second terminal/second `es_client` call — don't
kill the watch to do it.

---

## 8. `--skip-gate`: useful, but demonstrated to produce wrong answers

Bypassing `gate.py` means *any* `tier=2` candidate fires immediately,
including ones Gate's own rules exist specifically to reject:

- A **resource-only signal alone** (`cpu`/`memory`/`log_error_count` with no
  accompanying `p95_latency`/`error_rate`) can never open an incident under
  normal Gate rules — by design, since a resource metric alone doesn't
  prove customer impact. With `--skip-gate`, it fires anyway. Live example:
  it caught `payment/memory` drifting and confidently (0.8 confidence)
  matched it to `email`'s unrelated memory-leak runbook — a genuine wrong
  answer, on a signal Gate would have correctly ignored.
- Over one 9.5-minute unfiltered `--skip-gate` run, `frontend/memory` alone
  fired as a "sustained incident" **9 separate times**, almost certainly
  just normal JVM/GC sawtooth pattern, not real faults.

**Use `--skip-gate` to quickly exercise Remediator/Correlator's mechanics
against real live data. Don't use it to judge whether a specific diagnosis
is trustworthy** — that's exactly what Gate's rules are there to protect,
and this flag turns that protection off.

---

## 9. `probe-memory`: viewing, seeding, clearing

**View everything:**
```
GET probe-memory/_search
```
**View one runbook:**
```
GET probe-memory/_doc/<fault_class>__<service>
```
**Clear everything for a cold-start test** (only do this deliberately —
irreversible):
```
POST probe-memory/_delete_by_query
{ "query": { "match_all": {} } }
```

**The `(fault_class, service)` key collision is real, not theoretical.**
`adHighCpu` and `adManualGc` both key to `resource_exhaustion__ad`. If both
ever get seeded or graded correct through the normal path, one silently
overwrites the other. `catalog_runbook.py`'s `read_by_flag()`/
`read_local_all()` handle this correctly in Python (see their docstrings),
but `writer.py`'s `_runbook_id()` still computes the plain collision-prone
id — if you need a second entry for a colliding flag, seed it under a
distinct id manually (e.g. `resource_exhaustion__ad__adHighCpu`) rather
than letting the normal write path collide.

---

## 10. Reading results afterward

`watch_pipeline.py` only prints to console — it does not write a results
file (unlike `validate_pipeline.py`, which writes
`pipeline_validation_results.json`). If you need a durable record of a
`watch_pipeline.py` run, copy the console output, or check what it wrote to
`probe-memory` directly (section 9). `inspect_run.py` reads
`pipeline_validation_results.json`, so it only helps for `validate_pipeline.py`
runs, not `watch_pipeline.py` ones, as currently built.

---

## 11. Known-fixed bugs, so you don't re-diagnose them

All of these were found live, against this exact project, and are already
fixed in the code — if you see them, you're on an old checkout, not a new
bug:

- **`Unknown column [span.kind]`** — section 4's `SPAN_KIND_FIELD` issue.
- **`catalog/<flag>.yaml` "no entry" warning for `adHighCpu`** — the
  `(fault_class, service)` collision; fixed in `catalog_runbook.py`.
- **`Unknown column [id]` / `Unknown column [_score]`** in Remediator's
  stage 0/1 queries — needed `METADATA _id, _score` on the `FROM` clause;
  ES|QL doesn't expose document metadata as a queryable column otherwise.
- **`x_content_parse_exception` in the stage-1 RRF fallback** — a
  null-valued `dependency` produced an invalid `{"term": {"field": null}}`
  clause; fixed by omitting empty `should` clauses instead of passing null.
- **`KeyError: 'id'` on a repeated incident** — the fingerprint cache's
  `get_doc()` path doesn't include `id` in its returned `_source` the way
  stage 0/1's `RENAME _id AS id` does; fixed by injecting it from the
  already-known cache key.
- **Misleading `"No incident within N minutes"`** printed even after
  `--continuous` successfully handled many incidents — this message prints
  unconditionally when the time window expires, regardless of what happened
  during it. Read the whole log, not just the last line.

---

## 12. A realistic walkthrough

```bash
cd probe-pipeline
export ES_URL=... ES_API_KEY=... OPENAI_API_KEY=...
export SPAN_KIND_FIELD="kind" SPAN_KIND_SERVER_VALUE="Server"

# 1. Confirm telemetry is flowing at all (section 3's query, no service filter)
# 2. Ask whoever controls the demo stack which flag is on, and verify it's
#    real in the data (section 3) before watching.
# 3. If the floors look like they'll block it (section 6), decide up front
#    whether to lower them for this test.

python watch_pipeline.py --flag adFailure --use-openai --min-error-count 2 --continuous
```
Watch the console. A `memory_hit` or a real `Correlator` diagnosis means it
worked. If nothing fires within a few minutes despite confirmed-real
signal, re-check section 6's floors and section 4's field mapping before
assuming something else is wrong.
