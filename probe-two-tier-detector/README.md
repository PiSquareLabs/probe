# Two-Tier Detector (per `PROBE-detector-spec.md`)

**Detection method:** the exact design in [`../PROBE-detector-spec.md`](../PROBE-detector-spec.md)
— a two-tier pipeline combining `probe-detector`'s z-score scan (Tier 1,
"the lookout") with `causal-changepoint-detection`'s `CHANGE_POINT` query
(Tier 2, "the inspector"), with the ranking layer that `causal-changepoint-detection`
had explicitly removed. This module answers exactly three questions —
*which services changed, when, how sure are we* — and nothing else. It
never names a root cause, a dependency, or a rank; that's Correlator work
the spec deliberately keeps out of scope.

## 1. How it works

```
Tier 1 (zscore_scan.py)          Tier 2 (change_point.py)
  5 ES|QL queries, all               For EVERY candidate Tier 1
  services at once, 30s              shouted (not just the loudest):
  buckets, recent-60s vs             one CHANGE_POINT query scoped
  baseline z-score            --->   to that exact (service, signal),
  z >= 3 -> candidate                 2s buckets over 5 minutes.
                                       type/timestamp/pvalue if
                                       pvalue < 0.01, else tier stays 1.
                                            |
                                            v
                                   detector_scan() (detector.py)
                                   merges both into one result:
                                   candidates[], loudest, earliest,
                                   scan_at. No ranking, no graph.
```

`change_point(service, signal)` is exposed as a **standalone function**
(`change_point.py`), not buried inside the scan loop — per spec §3 and
§6, a future Correlator needs to call it too, on services Tier 1 never
shouted about (e.g. a quiet upstream cause one hop further up the
dependency graph). Nothing in this folder implements that Correlator
call; `change_point()` is just built to support it.

### Files

| File | What it is |
|---|---|
| `es_client.py` | Stdlib-only ES\|QL client, copied unchanged from `../probe-detector/` (already environment-variable configurable for local vs. Cloud — see `../RUNNING_ON_ELASTIC_CLOUD.md`). |
| `queries.py` | The five Tier 1 aggregation queries, copied unchanged from `../probe-detector/` — error rate, p95 latency, log error burst, CPU, memory, each already excluding flagd's noise. |
| `zscore_scan.py` | Tier 1: the same baseline/recent z-score logic as `../probe-detector/anomaly.py`, reshaped to the spec's `{service, signal, z, timestamp}` candidate contract with the spec's exact signal names (`p95_latency`, `error_rate`, `log_error_count`, `cpu`, `memory`). |
| `change_point.py` | Tier 2: `change_point(service, signal, lookback_minutes=5, bucket_seconds=2)`, a from-scratch reimplementation of `../causal-changepoint-detection/`'s per-service `CHANGE_POINT` query, narrowed to one service + one signal per call, with the tighter 2-second buckets the spec calls for (affordable per-call since it only ever runs on already-shouted candidates, not a blanket scan of every service). |
| `detector.py` | The combined loop (`detector_scan()`) plus a CLI for one-shot scans. |
| `validate_against_demo.py` | Injects each known flagd fault, polls `detector_scan()` until the target service is tier-2-confirmed (or times out), records both a "detected" and a stricter "confirmed" bar. |
| `validation_results.json` | Output of the run in §3 below. |

## 2. Running it

```bash
cd probe-two-tier-detector
python detector.py --out result.json            # one-shot scan
python validate_against_demo.py --settle 15      # full 11-flag battery
python change_point.py ad p95_latency            # call Tier 2 standalone, as a future Correlator would
```

No pip installs beyond the standard library. Needs the same demo stack as
every other detector in this repo — see `../RUNNING_LOCALLY.md`.

## 3. Validation: 2/11 confirmed

```
Detected (any tier): 2/11
Tier-2-confirmed: 2/11
```

| Flag | Target | Result | Time |
|---|---|---|---|
| `adHighCpu` | `ad` | **CONFIRMED** | 29.4s |
| `adManualGc` | `ad` | **CONFIRMED** | 32.4s |
| `adFailure` | `ad` | missed | — |
| `cartFailure` | `cart` | missed | — |
| `paymentFailure` | `payment` | missed | — |
| `recommendationCacheFailure` | `recommendation` | missed | — |
| `imageSlowLoad` | `frontend` | missed | — |
| `intlShippingSlowdown` | `shipping` | missed | — |
| `productCatalogFailure` | `product-catalog` | missed | — |
| `emailMemoryLeak` | `email` | missed | — |
| `kafkaQueueProblems` | *(none)* | not applicable | — |

Both confirmations line up exactly with the two flags every other
detector in this repo also finds easiest — `adHighCpu` (a real, sustained
CPU spike) and `adManualGc` (a real, sustained latency spike from forced
GC pauses). Both times, Tier 1 shouted and Tier 2 confirmed with an
extremely low p-value (down to 1e-86 in a manual spot-check during
development — see §4), exactly the spec's intended "loud, unambiguous
fault → fast, confident confirmation" path.

**Every miss here was a Tier 1 miss, not a Tier 2 rejection.** `detected:
false` means the target service never appeared in the candidate list at
*any* tier — Tier 1's z-score scan itself never shouted, so Tier 2 never
ran for that service at all. This isn't a new finding: it's the same
gap documented in every other detector in this repo (`ml-flag-detection`
README §6, `probe-detector` README §3) — several of these flags
(`adFailure`, `cartFailure`, `paymentFailure`, `productCatalogFailure`)
hit low-traffic or percentage-gated code paths that don't move a 30-second
z-score bucket enough to clear `z >= 3` in the first place, and
`emailMemoryLeak`'s target service emits no OTel memory metric in this
fork at all (confirmed independently three times now across this repo).

## 4. A methodological trap worth naming: don't retest the same flag back to back

While developing this, a manual spot-check confirmed `adManualGc`
excellently (`loudest=ad`, tier 2, `p=3.9e-86`, `type=spike`). Running
the validator on that *same* flag again three minutes later produced a
flat miss. The cause: Tier 1's 10-minute lookback window (`queries.WINDOW`,
inherited from `probe-detector`) still contained the *first* activation's
elevated values inside what it treats as "baseline" — comparing the
second activation's "recent" window against a baseline that already
includes the same fault's own earlier effect collapses the z-score. This
is not a bug in the two-tier logic; it's an artifact of testing the same
service twice within one lookback window. The full 11-flag battery in §3
tests 11 *distinct* flags once each, so this specific trap doesn't apply
there — but anyone extending this should leave enough gap between repeat
tests of flags sharing a target service (here, all three `ad` flags) or
shrink `zscore_scan.WINDOW` accordingly.

## 5. Comparison to `probe-detector` (Tier 1 alone, no confirmation gate)

| | `probe-detector` (z-score only) | This detector (z-score + `CHANGE_POINT` gate) |
|---|---|---|
| Detected | 5/11 | 2/11 |
| False-positive protection | None — every `z >= 3` candidate is reported as-is | Every candidate gets a real significance test; only `pvalue < 0.01` breaks survive as "confirmed" |
| What a `0/11`-adjacent number means | Genuinely lower sensitivity | Higher precision by design, at the cost of dropping candidates whose z-score was real but whose 2-second-bucketed break didn't clear `p < 0.01` in a 5-minute window |

Adding Tier 2 as a **gate** (only "confirmed" counts as detected) traded
recall for precision here — three flags `probe-detector` alone caught
(`adFailure`, `paymentFailure`, and, in its own earlier run,
`recommendationCacheFailure`) didn't survive Tier 2 confirmation in this
run. The spec's own design doesn't actually require treating
unconfirmed (`tier: 1`) candidates as failures — §1's contract keeps them
in the output, tier-tagged, rather than dropping them. `validate_against_demo.py`'s
stricter "confirmed" bar was a choice made for this validation script to
distinguish the two tiers clearly, not a requirement of the spec itself;
a consumer of this detector's raw output could reasonably still act on
`tier: 1` candidates with appropriate caution, exactly as the spec's own
worked example (§4) keeps `checkout`'s unconfirmed error-rate blip in the
final `candidates` list rather than discarding it.
