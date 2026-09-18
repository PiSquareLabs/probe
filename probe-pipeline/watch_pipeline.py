"""Watches the real Detector and runs whatever incident it finds through
Gate -> Remediator -> Correlator (-> Grader -> Writer if you tell it the
truth) -- with ZERO flagd editing. For exactly this split: someone else
(a teammate) injects the fault on their own machine, and you run the
downstream pipeline yourself against the same shared Elasticsearch,
from wherever you actually are.

Unlike validate_pipeline.py, this script never touches demo.flagd.json --
it has no idea what's about to be injected, or when. It just polls the
real Detector continuously and reacts the moment the Gate opens an
incident, same as a real running harness would.

Usage:
    export ES_URL=... ES_API_KEY=...
    export OPENAI_API_KEY=...              # optional, for real confirm/reasoning

    python watch_pipeline.py                          # watch until an incident, or Ctrl+C
    python watch_pipeline.py --minutes 5               # give up after 5 minutes of quiet
    python watch_pipeline.py --flag adFailure          # also grade against catalog/adFailure.yaml
                                                        # and write to probe-memory
    python watch_pipeline.py --continuous              # keep watching after each incident,
                                                        # instead of exiting after the first
    python watch_pipeline.py --skip-gate                # bypass Gate entirely -- react to any
                                                        # tier=2 candidate immediately, no
                                                        # sustained/intermittent persistence
                                                        # wait. TESTING ONLY -- see main()'s
                                                        # warning when this is passed.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog_runbook
import correlator
import detector_bridge
import es_client
import gate
import grader
import remediator
import writer
from schemas import Decision, Fingerprint

POLL_INTERVAL = 12


def _status_code_breakdown(service: str, cp_time: str | None) -> list[dict]:
    """HTTP/gRPC status codes for `service`'s own SERVER spans in the
    5 minutes before the change point -- cheap (one query, no join),
    but not previously extracted anywhere; added for --enrich-search.
    """
    if cp_time is None:
        return []
    query = f"""
        FROM {correlator.TRACES_DATA_STREAM}
        | WHERE service.name == ?service AND {es_client.SPAN_KIND_FIELD} == "{es_client.SPAN_KIND_SERVER_VALUE}"
          AND @timestamp > ?cp_time - 5 minutes AND @timestamp <= ?cp_time
        | STATS count = COUNT(*) BY status.code
        | SORT count DESC
        | LIMIT 5
    """
    try:
        return es_client.esql(query, {"service": service, "cp_time": cp_time})
    except es_client.EsqlError:
        return []


def enrich_fingerprint_and_symptom(trigger, raw: dict, correlator_instance: "correlator.Correlator"):
    """Pre-search enrichment: gathers what correlator.py normally only
    gathers *after* a memory_miss (dependency, correlated attributes,
    recent changes, graph rank) plus two cheap additions (pvalue/z from
    the trigger itself, and a status-code breakdown), and folds them
    into the Fingerprint/symptom the Remediator searches with.

    This is a real cost tradeoff, not a free improvement: it adds
    2-3 extra ES|QL queries to *every* incident, not just the misses
    PROBE-runbook-search-cases.md's "cheap before expensive" design
    intended them for (deepest_span/recent_changes/graph_rank were
    built to run only after stage 0/1 already missed). Opt-in via
    --enrich-search for exactly that reason -- see main()'s warning.
    """
    deepest_span, correlated = correlator.Correlator._deepest_span_and_correlated(raw["loudest"], trigger.timestamp)
    recent_changes = correlator.Correlator._recent_changes(trigger.timestamp)
    graph_rank = correlator_instance._graph_rank(raw["candidates"])
    status_codes = _status_code_breakdown(trigger.service, trigger.timestamp)

    dependency = deepest_span["dependency"] if deepest_span else None

    fingerprint = Fingerprint(
        change_point=trigger.type, metric=trigger.signal, loudest_service=raw["loudest"], dependency=dependency,
    )

    parts = [f"{trigger.service} {trigger.signal} {trigger.type}", f"loudest {raw['loudest']}"]
    if dependency:
        parts.append(f"dependency {dependency}")
    if correlated:
        parts.append(f"correlated {', '.join(correlated)}")
    if recent_changes:
        parts.append(f"{len(recent_changes)} recent change(s) in probe-changes")
    if graph_rank:
        top = graph_rank[0]
        parts.append(f"graph-top {top['service']} (score {top['score']})")
    if status_codes:
        codes = ", ".join(f"{r.get('status.code')}={r['count']}" for r in status_codes)
        parts.append(f"status codes: {codes}")
    if trigger.pvalue is not None:
        parts.append(f"pvalue={trigger.pvalue:.2e}")
    parts.append(f"z={trigger.z}")
    symptom = ", ".join(parts)

    return fingerprint, symptom


def handle_incident(decision, raw, remediator_instance, correlator_instance, truth_flag: str | None,
                     use_openai: bool, enrich_search: bool = False) -> None:
    print(f"\n{'=' * 70}")
    print(f"INCIDENT: kind={decision.kind} pattern={decision.pattern}  loudest={raw['loudest']}")
    print(f"  trigger: {[(c.service, c.signal, c.type) for c in decision.trigger]}")
    print(f"{'=' * 70}")

    trigger = decision.trigger[0]
    if enrich_search:
        fingerprint, symptom = enrich_fingerprint_and_symptom(trigger, raw, correlator_instance)
        print(f"\n(--enrich-search) fingerprint.dependency={fingerprint.dependency!r}")
        print(f"(--enrich-search) symptom: {symptom}")
    else:
        fingerprint = Fingerprint(
            change_point=trigger.type, metric=trigger.signal, loudest_service=raw["loudest"], dependency=None,
        )
        symptom = f"{trigger.service} {trigger.signal} {trigger.type}, loudest {raw['loudest']}"

    rem_out = remediator_instance.remediate(decision, fingerprint, symptom)
    print(f"\nRemediator: path={rem_out.path}")
    print(f"  timings_ms={rem_out.timings_ms}")
    print(f"  confirm={rem_out.confirm}")
    print(f"  candidates considered: {[c.get('id') for c in rem_out.candidates]}")
    if rem_out.ruled_out:
        print(f"  ruled_out: {[(r.proposed_fault_class, r.proposed_service) for r in rem_out.ruled_out]}")

    if rem_out.path == "memory_miss":
        diagnosis, evidence = correlator_instance.correlate(decision, raw, rem_out, symptom)
        print(f"\nCorrelator:")
        for key, value in evidence.items():
            print(f"  {key}: {value}")
        print(f"\n  diagnosis candidates: {[(c.fault_class, c.service, c.confidence) for c in diagnosis.candidates]}")
        print(f"  root_cause: {diagnosis.root_cause}")
        print(f"  steps: {diagnosis.steps}")
    else:
        rb = rem_out.runbook
        from schemas import Diagnosis, DiagnosisCandidate
        diagnosis = Diagnosis(
            candidates=[DiagnosisCandidate(rb["fault_class"], rb["service"], rem_out.confirm.get("confidence", 0.0))],
            root_cause=rb.get("root_cause", ""), symptom=symptom, steps=rb.get("steps", []), source="memory_hit",
        )
        print(f"\nMemory hit -- runbook is the diagnosis:")
        print(f"  {rb['fault_class']}/{rb['service']}: {rb.get('root_cause')}")

    if truth_flag:
        truth = catalog_runbook.read_by_flag(truth_flag)
        if truth is None:
            print(f"\nNo catalog/{truth_flag}.yaml entry -- can't grade.")
            return
        probe_run = grader.grade_incident(diagnosis, rem_out, truth.fault_class, truth.service)
        print(f"\nGrader: correct_at1={probe_run.correct_at1}  correct_at3={probe_run.correct_at3}  "
              f"abstained={probe_run.abstained}  (truth={truth.fault_class}/{truth.service})")

        w = writer.Writer(remediator_instance)
        write_result = w.write(probe_run, diagnosis, fingerprint, incident_id=f"watch_{int(time.time())}", symptom=symptom)
        print(f"Writer: {write_result}")
    else:
        print("\n(no --flag given -- skipping Grader/Writer, nothing graded or written)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=None, help="give up after this many minutes of no incident (default: watch forever)")
    ap.add_argument("--flag", default=None, help="grade against catalog/<flag>.yaml and write to probe-memory once an incident is found")
    ap.add_argument("--use-openai", action="store_true")
    ap.add_argument("--continuous", action="store_true", help="keep watching after handling an incident, instead of exiting")
    ap.add_argument("--enrich-search", action="store_true",
                     help="Gather dependency/correlated-attributes/recent-changes/graph-rank/"
                          "status-codes BEFORE the Remediator search, not just after a miss for the "
                          "Correlator's reasoning call. Adds 2-3 extra ES|QL queries to every "
                          "incident (not just misses) -- a real cost tradeoff against "
                          "PROBE-runbook-search-cases.md's 'cheap before expensive' design. Opt-in "
                          "for exactly that reason.")
    ap.add_argument("--min-spans-per-bucket", type=int, default=None)
    ap.add_argument("--min-error-count", type=int, default=None,
                     help="override zscore_scan.py's MIN_ERROR_COUNT (default 5) -- for a real but "
                          "thin error signal (e.g. ~10%% of modest traffic) that never accumulates "
                          "5 raw errors in the recent 30s window even though it's genuinely elevated")
    ap.add_argument("--skip-gate", action="store_true",
                     help="TESTING ONLY: bypass gate.py entirely. Any tier=2 candidate is treated "
                          "as an immediate incident, no sustained/intermittent persistence check. "
                          "This is not how the real pipeline behaves -- a real deployment must go "
                          "through the Gate's rules (contract #2) to avoid opening incidents on "
                          "noise. Use this only to exercise Remediator/Correlator against real "
                          "tier=2 signals that Gate's persistence rules keep missing the timing on.")
    args = ap.parse_args()

    if not args.use_openai:
        print("WARNING: --use-openai not passed -- expect path=memory_miss and fault_class='unknown' always.\n")
    if args.skip_gate:
        print("WARNING: --skip-gate passed -- Gate's sustained/intermittent rules are bypassed "
              "entirely. Any tier=2 candidate fires immediately, including ones Gate would "
              "correctly classify as noise. Testing only.\n")

    confirm_fn = reasoning_fn = None
    if args.use_openai:
        import llm_openai
        confirm_fn, reasoning_fn = llm_openai.confirm, llm_openai.reason

    streak_state: dict = {}
    gate_instance = gate.Gate()
    remediator_instance = remediator.Remediator(confirm_fn=confirm_fn) if confirm_fn else remediator.Remediator()
    correlator_instance = correlator.Correlator(reasoning_fn=reasoning_fn) if reasoning_fn else correlator.Correlator()
    detector_kwargs = {}
    if args.min_spans_per_bucket:
        detector_kwargs["min_spans_per_bucket"] = args.min_spans_per_bucket
    if args.min_error_count is not None:
        detector_kwargs["min_error_count"] = args.min_error_count

    # If --flag is given, only react to an incident that actually
    # touches that flag's target service (per catalog/<flag>.yaml) --
    # otherwise the first unrelated incident (noise, a different
    # low-volume signal crossing threshold) gets grabbed and graded
    # against the wrong truth entirely, silently.
    expect_service = None
    if args.flag:
        truth = catalog_runbook.read_by_flag(args.flag)
        if truth is None:
            print(f"WARNING: no catalog/{args.flag}.yaml entry -- can't filter by expected service, "
                  f"will react to ANY incident and try (and fail) to grade it against '{args.flag}'.")
        else:
            expect_service = truth.service
            print(f"Filtering to incidents touching '{expect_service}' (catalog/{args.flag}.yaml's target).")

    deadline = time.time() + args.minutes * 60 if args.minutes else None
    print(f"Watching (no flagd edits from here) -- poll every {POLL_INTERVAL}s"
          + (f", giving up after {args.minutes} min" if args.minutes else ", Ctrl+C to stop") + " ...")

    try:
        while deadline is None or time.time() < deadline:
            raw = detector_bridge.scan(streak_state, **detector_kwargs)
            if raw:
                if args.skip_gate:
                    tier2 = [c for c in raw["candidates"] if c.tier == 2]
                    decision = Decision(kind="incident", pattern="sustained", trigger=tier2) if tier2 else Decision(kind="watch", pattern=None, trigger=[])
                else:
                    decision = gate_instance.evaluate(raw)
                if decision.kind == "incident":
                    touches_expected = expect_service is None or any(c.service == expect_service for c in decision.trigger)
                    if not touches_expected:
                        print(f"  (incident on {[c.service for c in decision.trigger]}, not '{expect_service}' -- ignoring, still watching)")
                    else:
                        handle_incident(decision, raw, remediator_instance, correlator_instance, args.flag,
                                         args.use_openai, enrich_search=args.enrich_search)
                        if not args.continuous:
                            return
                elif decision.kind == "transient":
                    print(f"  (transient: {[(c.service, c.signal) for c in decision.trigger]})")
            time.sleep(POLL_INTERVAL)
        print(f"\nNo incident within {args.minutes} minutes.")
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
