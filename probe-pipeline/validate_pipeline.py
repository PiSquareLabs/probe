"""Proves the wired-together pipeline (Detector -> Gate -> Remediator ->
Correlator -> Grader -> Writer) actually does something sensible against
the real Astronomy Shop, the same way every other detector folder proves
itself: inject a real flagd fault, wait, run the pipeline, reset the
flag, log every stage's output, write a JSON results file.

This is NOT probe-two-tier-detector-v3/validate_against_demo.py's job
(that already validates the Detector alone, 5 cycles, tier-2 confirm
rate) -- this script assumes the Detector works and asks the question
one level up: does an incident actually flow through Gate/Remediator/
Correlator/Grader/Writer and end with a sane, gradeable result.

Ground truth for grading comes from catalog/*.yaml via
catalog_runbook.read_by_flag() -- the same hand-written (fault_class,
service) pairs used throughout this project, never from the LLM.

IMPORTANT -- read before running: unless --use-openai is passed (and
OPENAI_API_KEY is set), both LLM calls are the fail-closed stubs, so
every incident will come back path=memory_miss and
diagnosis.top1.fault_class="unknown" no matter what's injected. That's
expected, not a bug -- see this script's own printed warning. Pass
--use-openai to get a real answer worth grading.

Usage:
    export ES_URL=... ES_API_KEY=...
    python setup_probe_memory.py               # once, if not already done
    python validate_pipeline.py                       # all catalog flags, 1 cycle each
    python validate_pipeline.py --only paymentFailure  # one flag
    python validate_pipeline.py --use-openai           # real confirm/reasoning calls
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog_runbook
import detector_bridge
import gate
import grader
import remediator
import writer
from schemas import Fingerprint

REPO_ROOT = Path(__file__).resolve().parent.parent
FLAGD_PATH = REPO_ROOT / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"

# (enabled_value, target_service) -- target is None for flags the Detector
# structurally can't see (no direct trace/metric signal in this fork,
# per DETECTORS.md); those are skipped rather than run to a guaranteed miss.
FLAG_VARIANTS = {
    "adFailure": ("on", "ad"),
    "adHighCpu": ("on", "ad"),
    "adManualGc": ("on", "ad"),
    "cartFailure": ("100%", "cart"),
    "paymentFailure": ("100%", "payment"),
    "paymentUnreachable": ("on", "checkout"),
    "recommendationCacheFailure": ("on", "recommendation"),
    "imageSlowLoad": ("10sec", "frontend"),
    "intlShippingSlowdown": ("10sec", "shipping"),
    "productCatalogFailure": ("on", "product-catalog"),
    "failedReadinessProbe": ("on", "cart"),
    "emailMemoryLeak": ("10x", None),  # no OTel memory metric in this fork
    "kafkaQueueProblems": ("on", None),  # Kafka itself isn't traced
}

POLL_INTERVAL = 12
MAX_WAIT = 150


def set_flag(name: str, variant: str) -> None:
    data = json.loads(FLAGD_PATH.read_text())
    data["flags"][name]["defaultVariant"] = variant
    FLAGD_PATH.write_text(json.dumps(data, indent=2))


def reset_flag(name: str) -> None:
    set_flag(name, "off")


def wait_for_incident(streak_state: dict, gate_instance: gate.Gate, target_service: str, deadline: float):
    """Polls the real Detector until the Gate opens an incident whose
    trigger touches `target_service`, or the deadline passes. Returns
    (decision_or_None, detector_output_or_None).
    """
    while time.time() < deadline:
        raw = detector_bridge.scan(streak_state)
        if raw:
            decision = gate_instance.evaluate(raw)
            if decision.kind == "incident" and any(c.service == target_service for c in decision.trigger):
                return decision, raw
        time.sleep(POLL_INTERVAL)
    return None, None


def run_one_flag(flag: str, variant: str, target_service: str, settle: int, streak_state: dict,
                  gate_instance: gate.Gate, remediator_instance: remediator.Remediator,
                  correlator_instance, symptom: str) -> dict:
    log: dict = {"flag": flag, "variant": variant, "target_service": target_service}

    print(f"=== {flag} (variant={variant!r}, target={target_service}) ===")
    set_flag(flag, variant)
    injected_at = time.time()
    print(f"  injected at {datetime.now(timezone.utc).isoformat()}, settling {settle}s...")
    time.sleep(settle)

    decision, raw = wait_for_incident(streak_state, gate_instance, target_service, injected_at + MAX_WAIT)
    reset_flag(flag)

    if decision is None:
        print(f"  Gate never opened an incident for {target_service} within {MAX_WAIT}s")
        log.update({"gate_kind": None, "seconds_to_incident": None})
        return log

    seconds_to_incident = time.time() - injected_at
    log["seconds_to_incident"] = round(seconds_to_incident, 1)
    log["gate_kind"] = decision.kind
    log["gate_pattern"] = decision.pattern
    print(f"  Gate: incident/{decision.pattern} after {seconds_to_incident:.1f}s")

    trigger = decision.trigger[0]
    fingerprint = Fingerprint(
        change_point=trigger.type, metric=trigger.signal, loudest_service=raw["loudest"], dependency=None,
    )
    rem_out = remediator_instance.remediate(decision, fingerprint, symptom)
    log["remediator_path"] = rem_out.path
    log["remediator_timings_ms"] = rem_out.timings_ms
    print(f"  Remediator: {rem_out.path}  (timings_ms={rem_out.timings_ms})")

    truth = catalog_runbook.read_by_flag(flag)
    if truth is None:
        print(f"  no catalog/{flag}.yaml entry -- can't grade, stopping here")
        log["graded"] = False
        return log

    if rem_out.path == "memory_miss":
        diagnosis, evidence = correlator_instance.correlate(decision, raw, rem_out, symptom)
        log["diagnosis_top1"] = {"fault_class": diagnosis.top1.fault_class, "service": diagnosis.top1.service, "confidence": diagnosis.top1.confidence}
        log["evidence_keys_present"] = {k: bool(v) for k, v in evidence.items()}
        print(f"  Correlator: top1={diagnosis.top1.fault_class}/{diagnosis.top1.service} (confidence={diagnosis.top1.confidence})")
    else:
        # memory_hit -- the Remediator's own runbook IS the diagnosis;
        # correlator.py never runs on a hit (contract #4). fault_class/
        # service come from the runbook doc's own fields (writer.py
        # stores both explicitly), never from `truth` -- that's read
        # only by grade_incident() below, after this diagnosis is built.
        rb = rem_out.runbook
        from schemas import Diagnosis, DiagnosisCandidate
        diagnosis = Diagnosis(
            candidates=[DiagnosisCandidate(rb["fault_class"], rb["service"], rem_out.confirm.get("confidence", 0.0))],
            root_cause=rb.get("root_cause", ""), symptom=symptom, steps=rb.get("steps", []), source="memory_hit",
        )

    probe_run = grader.grade_incident(diagnosis, rem_out, truth.fault_class, truth.service)
    log["graded"] = True
    log["correct_at1"] = probe_run.correct_at1
    log["abstained"] = probe_run.abstained
    print(f"  Grader: correct_at1={probe_run.correct_at1}  abstained={probe_run.abstained}  (truth={truth.fault_class}/{truth.service})")

    w = writer.Writer(remediator_instance)
    write_result = w.write(probe_run, diagnosis, fingerprint, incident_id=f"validate_{flag}_{int(injected_at)}", symptom=symptom)
    log["writer_result"] = write_result
    print(f"  Writer: {write_result}")
    print()
    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--settle", type=int, default=15)
    ap.add_argument("--use-openai", action="store_true", help="wire llm_openai.confirm/reason instead of the fail-closed stubs")
    args = ap.parse_args()

    if not args.use_openai:
        print(
            "WARNING: --use-openai not passed. Both LLM calls are fail-closed "
            "stubs -- every incident will come back path=memory_miss and "
            "fault_class='unknown', regardless of what's injected. That's "
            "expected, not a failure of the pipeline's control flow.\n"
        )

    confirm_fn = None
    reasoning_fn = None
    if args.use_openai:
        if not os.environ.get("OPENAI_API_KEY"):
            print("ERROR: --use-openai passed but OPENAI_API_KEY is not set."); sys.exit(1)
        import llm_openai
        confirm_fn, reasoning_fn = llm_openai.confirm, llm_openai.reason

    flags_to_test = args.only or [f for f, (_, target) in FLAG_VARIANTS.items() if target is not None]

    streak_state: dict = {}
    gate_instance = gate.Gate()
    remediator_instance = remediator.Remediator(confirm_fn=confirm_fn) if confirm_fn else remediator.Remediator()
    import correlator
    correlator_instance = correlator.Correlator(reasoning_fn=reasoning_fn) if reasoning_fn else correlator.Correlator()

    results = []
    for flag in flags_to_test:
        variant, target = FLAG_VARIANTS[flag]
        if target is None:
            print(f"=== {flag}: skipped, no detectable target service (see DETECTORS.md) ===\n")
            continue
        results.append(
            run_one_flag(flag, variant, target, args.settle, streak_state, gate_instance,
                         remediator_instance, correlator_instance, symptom=f"{flag} injected on {target}")
        )

    print("=" * 70)
    for r in results:
        tag = "GRADED-CORRECT" if r.get("correct_at1") else ("GRADED-WRONG" if r.get("graded") else "NOT-DETECTED")
        print(f"{r['flag']:28s} {tag:16s} gate={r.get('gate_kind')!s:10s} path={r.get('remediator_path')!s:14s} secs={r.get('seconds_to_incident')}")

    out_path = Path("pipeline_validation_results.json")
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
