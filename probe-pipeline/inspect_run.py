"""Reads everything a validate_pipeline.py run produced -- for one flag
or all of them -- and prints one consolidated report per flag: the
Remediator's full search result, the Correlator's full evidence block
and diagnosis, the Grader's verdict, and the actual runbook document
currently sitting in `probe-memory` (fetched fresh from Elasticsearch,
not just whatever validate_pipeline.py logged at write time -- if the
runbook has since been updated, demoted, or human-verified, this shows
its *current* state).

Everything here is read-only, off the graded path -- same status as
github_runbook.py/catalog_runbook.py. This is for a human to look at
after a run, not something remediator.py/correlator.py import.

Usage:
    export ES_URL=... ES_API_KEY=...
    python inspect_run.py                        # every flag in the results file
    python inspect_run.py --only paymentFailure   # one flag
    python inspect_run.py --file other_results.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import es_client

DEFAULT_RESULTS_FILE = Path(__file__).resolve().parent / "pipeline_validation_results.json"


def _section(title: str) -> None:
    print(f"\n{'-' * 70}\n{title}\n{'-' * 70}")


def _fetch_runbook(runbook_id: str) -> dict | None:
    try:
        return es_client.get_doc("probe-memory", runbook_id)
    except es_client.EsqlError as e:
        print(f"  (could not fetch probe-memory/{runbook_id}: {e})")
        return None


def print_report(entry: dict) -> None:
    flag = entry["flag"]
    print("=" * 70)
    print(f"FLAG: {flag}   (variant={entry.get('variant')!r}, target={entry.get('target_service')})")
    print("=" * 70)

    _section("Detector / Gate")
    if entry.get("gate_kind") is None:
        print("  Gate never opened an incident -- nothing downstream ran.")
        return
    print(f"  gate_kind={entry['gate_kind']}  pattern={entry.get('gate_pattern')}  "
          f"seconds_to_incident={entry.get('seconds_to_incident')}")

    _section("Remediator")
    print(f"  path={entry.get('remediator_path')}")
    print(f"  timings_ms={entry.get('remediator_timings_ms')}")
    print(f"  confirm={entry.get('remediator_confirm')}")
    candidates = entry.get("remediator_candidates") or []
    print(f"  candidates considered ({len(candidates)}):")
    for c in candidates:
        print(f"    - {c.get('id')}  status={c.get('status')}  occurrences={c.get('occurrences')}")
    ruled_out = entry.get("remediator_ruled_out") or []
    if ruled_out:
        print(f"  ruled_out for this symptom ({len(ruled_out)}):")
        for r in ruled_out:
            print(f"    - {r['proposed_fault_class']}/{r['proposed_service']}  (incident {r['incident']})")

    if entry.get("remediator_path") == "memory_miss":
        _section("Correlator")
        evidence = entry.get("evidence", {})
        for key, value in evidence.items():
            print(f"  {key}:")
            print(f"    {json.dumps(value, default=str)}")
        print()
        print(f"  diagnosis candidates: {entry.get('diagnosis_candidates')}")
        print(f"  root_cause: {entry.get('diagnosis_root_cause')}")
        print(f"  steps: {entry.get('diagnosis_steps')}")

    if not entry.get("graded"):
        print("\n  Not graded (no catalog/<flag>.yaml truth entry found).")
        return

    _section("Grader")
    truth = entry.get("truth", {})
    print(f"  truth: {truth.get('fault_class')}/{truth.get('service')}")
    print(f"  correct_at1={entry.get('correct_at1')}  correct_at3={entry.get('correct_at3')}  abstained={entry.get('abstained')}")

    _section("Writer + current probe-memory state")
    write_result = entry.get("writer_result", {})
    print(f"  writer_result: {write_result}")
    runbook_id = write_result.get("runbook_id") or write_result.get("demoted")
    if runbook_id:
        rb = _fetch_runbook(runbook_id)
        if rb is None:
            print(f"  probe-memory/{runbook_id}: not found (was it deleted, or a different cluster than this run used?)")
        else:
            print(f"  probe-memory/{runbook_id} (live, fetched now):")
            print(f"    status={rb.get('status')}  occurrences={rb.get('occurrences')}  failed_reuses={rb.get('failed_reuses')}  verified_by={rb.get('verified_by')}")
            print(f"    root_cause: {rb.get('root_cause')}")
            print(f"    steps: {rb.get('steps')}")
            print(f"    symptoms: {rb.get('symptoms')}")
            print(f"    signature: {rb.get('signature')}")
    else:
        print("  No runbook was written for this run (wrong grade + memory_miss, abstained, or ablation).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--file", type=Path, default=DEFAULT_RESULTS_FILE)
    args = ap.parse_args()

    if not args.file.exists():
        print(f"No results file at {args.file} -- run validate_pipeline.py first.")
        sys.exit(1)

    results = json.loads(args.file.read_text())
    if args.only:
        results = [r for r in results if r["flag"] in args.only]
        missing = set(args.only) - {r["flag"] for r in results}
        if missing:
            print(f"Not found in {args.file}: {sorted(missing)}")

    for entry in results:
        print_report(entry)
        print()


if __name__ == "__main__":
    main()
