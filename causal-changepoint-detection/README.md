# Causal Changepoint Detection

**Detection method:** statistical change-point detection (Elasticsearch's
ES|QL `CHANGE_POINT` command) run per service over a rolling window, with
the resulting anomalous services ranked by an empirically-derived service
call graph to name a single most-likely root cause — instead of a threshold
alert that fires on every service downstream of a failure and names none of
them specifically.

This is a second, independent detection method against the same Elastic
OpenTelemetry demo stack used by `../ml-flag-detection/` (a supervised
ML classifier). Where that one learns "which flag is active" from a labeled
training set, this one needs no training data or labels at all — it's a
live, unsupervised statistical + graph-based method that could run
continuously against production traffic.

---

## 1. How it works

```
Layer 1  Per-service health series
         ES|QL STATS ... BY bucket, for each service:
         p95 latency + error rate, in fixed time buckets.

Layer 2  CHANGE_POINT per service
         Run once per (service, metric) pair -- ES|QL's CHANGE_POINT
         needs a single ordered series, so this loops over services
         rather than trying one grouped query for all of them.
         Keep services where a change point's p-value < 0.05.
         -> a set of "candidates": services with a real, dated shift.

Layer 3  Service call graph
         Derived empirically from real span parent/child links
         (derive_service_graph.py), not hand-typed from memory of the
         demo's architecture. Stored in service_graph.json.

Layer 4  Causal ranking
         For each candidate, score = 0.6 * earliness + 0.4 * (how many
         OTHER candidates depend on it, per the graph), tie-broken by
         p-value. Highest score = named root cause.
```

### Real field names used (verified against the live index)

This stack ingests via the OTel-native mapping, **not** the classic Elastic
APM field set. If you've read generic Elastic APM docs/examples, the field
names there (`service.name`, `span.duration.us`, `event.outcome`,
`parent.id`) will not work here. The real fields, confirmed by querying the
live index directly:

| Generic APM name (won't work) | Actual field in this stack |
|---|---|
| `service.name` | `resource.attributes.service.name` |
| `span.duration.us` (microseconds) | `duration` (**nanoseconds**) |
| `event.outcome` | `attributes.event.outcome` |
| `parent.id` | `parent_span_id` |
| `span.id` | `span_id` |
| `trace.id` | `trace_id` |

### The flagd-noise trap (again)

Just like `../ml-flag-detection/` found: flagd's own OpenFeature client
constantly reconnects a streaming RPC
(`flagd.evaluation.v1.Service/EventStream`,
`.../ResolveBoolean`), and these spans (a) have absurdly long durations
(hours — they're long-lived stream connections, not requests) which blow up
percentile calculations, and (b) get tagged `event.outcome: failure` on
every reconnect. Every query in this folder filters these out with
`NOT STARTS_WITH(name, "flagd.evaluation")` and
`attributes.processor.event == "transaction"` (top-level spans only). Skip
either filter and every service looks like it has multi-minute latency and
a nonzero error rate at all times.

## 2. Files

| File | What it is |
|---|---|
| `derive_service_graph.py` | Pages through recent spans, joins each span to its parent by `parent_span_id`/`span_id` within the fetch window, and counts `caller_service -> callee_service` edges. Empirical, not hardcoded — the graph reflects what this fork's services actually call, which drifts across versions. |
| `service_graph.json` | Output of the above: 23 empirically-observed edges (regenerated with a 240-minute window once the stack had accumulated enough uptime to exercise `checkout -> payment`/`checkout -> email`, which an earlier 30-180 min pass never saw — no manual/guessed edges needed once traffic volume was sufficient). |
| `changepoint_detector.py` | The detector itself: Layers 1-2-4 above. Run this. |

## 3. Running it

Requires the same stack as `../ml-flag-detection/` (see that folder's
README §2 for how to bring it up) — needs Elasticsearch reachable at
`localhost:9200` with credentials in
`../opentelemetry-demo/elastic-start-local/.env`, and services actively
receiving traffic (the bundled k6 load-generator running is enough).

```bash
# 1. (Re)derive the call graph -- worth re-running if you change forks/versions,
#    or if you've been running long enough to exercise more code paths.
#    Checkout-adjacent edges (payment, email) are rare in the default
#    load-generator mix: a 30-60 min window missed them entirely, and even
#    240 min only caught 17 calls each. Use as wide a window as your stack's
#    uptime allows; the script pages up to 200k spans (40 pages x 5000),
#    oldest-first, so on a long-running stack a window wider than that cap
#    represents will just get truncated to its earliest portion -- bump
#    max_pages/page_size in the script if you need more.
python derive_service_graph.py --minutes 240

# 2. Run the detector
python changepoint_detector.py --lookback 15 --bucket 20
```

No pandas/requests dependency — stdlib only (`urllib`, `json`), following
the same memory-conscious pattern established in
`../ml-flag-detection/finish_remaining.py` after that project's host turned
out to be memory-constrained running this many containers at once.

`--verbose` prints real ES|QL errors per service instead of treating every
error as "no signal" — **use this while developing**; the two real bugs
found while building this (documented in §5) both silently produced "no
change points found" until `--verbose` was added.

## 4. Validation against a known, injected fault

Following the same "prove it before you trust it" approach as
`../ml-flag-detection/`: injected `adManualGc` (the same flag that gave the
ML classifier's cleanest signal — see that project's README §6) directly
into `demo.flagd.json`, waited for the load-generator to produce enough
post-injection traffic, and ran the detector.

**Layer 2 (CHANGE_POINT) worked immediately and unambiguously**: `ad`'s p95
latency jumped from the 8-450ms range to 19,000-95,000ms, and CHANGE_POINT
flagged it as a `spike` with p=6.4e-24 as soon as enough post-shift samples
existed (it needs a handful of buckets on both sides of the shift — the
very first check, run only ~1 minute after injection, correctly found
nothing yet because there wasn't enough post-shift data, which is the
correct, cautious behavior, not a bug).

**Layer 4 (causal ranking) correctly named `ad` in one run, but not
reliably.** Two runs a few minutes apart against the same live fault:

- Run 1: `ad`, `cart`, `currency` all showed a significant latency shift
  within the same ~40-second window, with no direct call-graph edge between
  any pair of them (all three are independent children of `frontend`, called
  in parallel, not chained). The ranking's earliness+graph score correctly
  named **`ad`** — matching the actual injected fault.
- Run 2 (~2 minutes later, same fault still active): the same three
  services were still anomalous, but this time `currency`'s p-value
  (3.5e-48) was more extreme than `ad`'s (1.8e-26), and the p-value
  tiebreaker (added after run 1 to make ties deterministic rather than an
  accident of list order) picked **`currency`** instead — the wrong answer.

### Why, and what this means

`ad`, `cart`, and `currency` have no call relationship to each other in the
derived graph — they're siblings, not a chain. When they degrade
*together* with no graph edge connecting them, the most likely explanation
isn't hidden coupling between the three; it's **host-level contention**: a
Java GC pause (`adManualGc` literally forces full GCs) can starve CPU for
sibling containers on the same Docker host for the fraction of a second it
runs, and this stack is already running two full OTel-demo deployments plus
Elasticsearch/Kibana on a resource-constrained 15GB machine (see
`../ml-flag-detection/README.md` for the extensive memory-pressure
troubleshooting that was needed just to collect the ML classifier's
dataset on this box). A p-value comparison between `ad` and `currency`
can't distinguish "which one is the true root cause" from "which one
happened to have a cleaner, less noisy baseline to measure a shift against"
— that's a property of each service's own traffic pattern, not evidence
about causality between them.

**This is a genuine limitation of the current causal-ranking heuristic**,
not a implementation bug: earliness-within-a-bucket and graph reachability
are real, useful signals when an anomaly's *effect* actually propagates
through call edges (e.g. a slow shared dependency making all its distinct
callers slow — the method should handle that pattern well, since "depended
on by N other candidates" is exactly built to catch it). It has no way to
tell "sibling A and sibling B both degraded because of a shared
resource neither calls" apart from "sibling A caused sibling B" when there's
no edge between them at all — both look identical to a call graph. Fixing
this properly would need either (a) a less contended host so simultaneous,
graph-unconnected shifts become rare enough that "no edge = probably
independent, or a shared out-of-graph cause" is a safe inference, or (b)
correlating with host-level container CPU/memory metrics (not currently
in this detector's feature set) to positively identify shared-resource
contention as a distinct cause category, separate from application-level
causality.

## 5. Bugs found and fixed while building this

Two bugs in `changepoint_detector.py` both manifested identically as "no
significant change points found" — indistinguishable from a genuinely quiet
system unless you go looking, which is exactly the trap a real anomaly
detector must not fall into (a detector that fails silently is worse than
one that's occasionally wrong loudly).

1. **Malformed ES|QL from string interpolation.** The first version passed
   a metric field and an aggregation function name separately and built
   `STATS m = {agg}({field})`, but was called with `agg="PERCENTILE(duration, 95)"`
   already containing its own parens — producing
   `PERCENTILE(duration, 95)(duration)`, a syntax error. The `except
   HTTPError: return []` handler treated this identically to "series too
   flat/short for CHANGE_POINT to say anything," which is a legitimate,
   common outcome — so the malformed query silently looked like "no
   anomaly" instead of erroring loudly. Fixed by taking one complete stat
   expression string per call instead of field+aggregation-name pieces, and
   by adding `--verbose` to print real errors instead of swallowing all of
   them identically.
2. **Wrong HTTP method.** `urllib.request` was told `method="GET"` for
   Elasticsearch's `/_query` endpoint, which only accepts POST — every
   query 405'd. This one is a rhyme of the same underlying problem: manual
   `curl -d '{...}'` testing during development defaults to POST and hid the
   bug completely; it only surfaced once `--verbose` was added and the
   script's actual HTTP client was exercised directly.

**Takeaway for future maintenance:** never let "series had nothing to say"
and "the query was wrong" collapse into the same code path silently. This
class of bug is exactly why the CHANGE_POINT layer's very first live test
(baseline, no fault injected, §4) needs to be read carefully -- "0 findings"
must mean "confirmed quiet," not "the detector is broken and reporting
quiet by default." Rerunning with `--verbose` after that first "0 findings"
result is what caught both bugs here.

## 5a. Full 11-flag validation battery: a worse, more informative result

The single-flag spot-check in §4 above (`adManualGc`, run in isolation)
was encouraging: `CHANGE_POINT` caught it in seconds with p<1e-24, and the
ranking named `ad` correctly at least once. Running the same
fault-injection methodology across **all 11 flags** back-to-back
(`validate_against_demo.py`, ~28 minutes total) told a different story:

```
Detected (in candidate set): 5/11
Correctly named as #1 root cause: 1/11
```

| Flag | Target | Result | Candidates seen | Top-ranked |
|---|---|---|---|---|
| `adFailure` | `ad` | missed | `cart`, `currency` | `cart` |
| `adHighCpu` | `ad` | detected, not named | `ad`, `cart`, `product-catalog` | `cart` |
| `adManualGc` | `ad` | detected, not named | `ad`, `cart`, `frontend`, `recommendation` | `cart` |
| `cartFailure` | `cart` | **named root cause** | `ad`, `cart`, `currency`, `recommendation` | `cart` |
| `paymentFailure` | `payment` | missed | `ad`, `cart`, `product-catalog`, `recommendation` | `recommendation` |
| `recommendationCacheFailure` | `recommendation` | missed | `ad`, `cart`, `currency`, `product-catalog` | `product-catalog` |
| `imageSlowLoad` | `frontend` | detected, not named | `ad`, `cart`, `currency`, `frontend`, `product-catalog`, `recommendation` | `product-catalog` |
| `intlShippingSlowdown` | `shipping` | missed | `ad`, `cart`, `currency`, `frontend`, `product-catalog`, `recommendation` | `ad` |
| `productCatalogFailure` | `product-catalog` | detected, not named | `ad`, `cart`, `currency`, `frontend`, `product-catalog`, `recommendation` | `ad` |
| `emailMemoryLeak` | `email` | missed | `ad`, `cart`, `product-catalog`, `recommendation` | `ad` |
| `kafkaQueueProblems` | *(none)* | n/a | — | — |

**The candidate lists are the real story here.** `ad`, `cart`,
`product-catalog`, `recommendation`, and `currency` show up as "anomalous"
in nearly *every* row, regardless of which fault — if any — was actually
injected into that service. That's not plausible as a real, independent
effect of 11 different faults; it's the signature of a shared confound
swamping the actual signal. Two candidate explanations, both plausible and
not mutually exclusive:

1. **This machine's ambient host contention got worse as the session went
   on.** Documented from the start in `../ml-flag-detection/README.md`:
   two full OTel-demo stacks plus Elasticsearch/Kibana on a 15GB host. By
   the time this battery ran (hours into the session), the noise floor
   had visibly risen compared to the earlier isolated spot-check.
2. **The validation methodology itself is heavier than `probe-detector`'s
   and may have contributed to the very load it was measuring.** Each
   scan here issues 24 sequential `CHANGE_POINT` queries (2 metrics x 12
   services), each one a nontrivial aggregation over a 15-minute window --
   substantially more Elasticsearch CPU per check than
   `../probe-detector/`'s 5 lightweight bucketed aggregations. Unlike
   `probe-detector/validate_against_demo.py`, which polls every 10s and
   reports genuine elapsed-time-to-detect, this validator only runs *one*
   scan per flag after a fixed 60s settle -- so every row's "seconds"
   column is really "fixed settle + fixed scan cost" (~110s), not an
   adaptive detection latency. That was a deliberate simplification to
   keep the battery's total runtime bounded, but it means this column
   isn't comparable to `probe-detector`'s per-flag timings, and it means a
   single scan under heavier concurrent load is more exposed to whatever
   contention is happening at that exact moment than a method that gets
   several independent tries.

**Honest conclusion:** the `CHANGE_POINT` *detection* layer (candidates
found at all) is doing real work -- 5/11 targets did show up, and the
service graph itself (`service_graph.json`) is empirically correct. But
the causal-*ranking* layer, validated at scale rather than on one
cherry-picked fault, is not reliable on this specific host under these
specific conditions: `cart` wins the ranking in 3 of the 5 "detected" rows
regardless of whether `cart` had anything to do with the actual fault,
which is a symptom of the earliness+graph-reachability heuristic latching
onto whichever service happens to have the noisiest recent baseline, not
onto genuine causality. This is a more useful result than a clean pass
would have been: it says specifically where this method needs work (either
a cleaner host, or a smarter ranking signal that discounts a candidate
whose "shift" turns out to correlate with host-level metrics rather than
its own request volume) rather than leaving "does the ranking work?" an
open question answered by one convenient example.

## 6. Comparison to the ML classifier (`../ml-flag-detection/`)

| | ML flag classifier | Causal changepoint detector |
|---|---|---|
| Needs labeled training data | Yes (65 labeled windows) | No |
| Tells you *which flag* | Yes, directly (that's the label) | No — tells you *which service*, which is what you'd actually see in production without flag-level ground truth |
| Statistical rigor | Standard train/test split, but only 5 samples/class | Formal significance test (CHANGE_POINT p-value) per finding |
| Root-cause narrative | Implicit in feature importances | Explicit: names one service with a stated reason |
| Failure mode found | Missing signal for some flags (§6 of that README) | Ambiguity between graph-unconnected siblings under host contention (§4 above) |
| Could run against unknown/new faults | No — only recognizes the 11 flags it was trained on | Yes — CHANGE_POINT doesn't need to know what kind of fault it's looking for |

Both are legitimate, complementary approaches; neither is "the" answer on
its own. A real production system would likely want the changepoint
detector's continuous, no-training-needed change *detection*, escalating
to something like the ML classifier (retrained periodically) or a human
for causal attribution when the changepoint layer's own ranking is
ambiguous, as it was in §4 above.
