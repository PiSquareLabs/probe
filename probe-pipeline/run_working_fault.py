"""The dashboard's "known-working" run button: injects one of
WORKING_FAULTS itself, then reuses watch_pipeline.py's own
handle_incident() with skip_gate=True baked in -- the one combination
empirically confirmed (this session, this local stack) to produce a
correct, fully graded result end to end: Detector -> Gate (bypassed,
see WORKING_FAULTS' own docstring for why) -> Remediator -> Grader ->
Writer, path=memory_hit, correct_at1=True.

watch_pipeline.py deliberately never edits demo.flagd.json (its whole
point is supporting fault injection from a different machine than the
one running the pipeline) -- this script is the one that does, for the
single-machine "click Run in the dashboard" case, reusing
watch_pipeline.py's incident-handling logic rather than duplicating it.

Usage:
    python run_working_fault.py --flag adHighCpu [--use-openai | --use-bedrock]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import correlator
import detector_bridge
import flagd_control
import gate
import pipeline_log as plog
import remediator
from schemas import Decision

FLAGD_PATH = flagd_control.FLAGD_PATH  # single source of truth, env-overridable via FLAGD_PATH
POLL_INTERVAL = 12
MAX_WAIT_SECONDS = 150

# Empirically confirmed via watch_pipeline.py --skip-gate --use-openai,
# with probe-memory seeded from catalog/*.yaml (seed_probe_memory_from_catalog.py):
# real Gate never opened an incident for any tested flag on this local
# stack's current traffic level within a single ~150s cycle (cartFailure,
# paymentFailure, recommendationCacheFailure, adFailure all missed
# real-Gate tests) -- but adHighCpu's cpu signal reliably crosses Tier 2
# confirmation within ~30-40s regardless, so with skip_gate=True it
# consistently produces a real memory_hit, confirm.match=True, and
# correct_at1=True. This is Gate bypassed, not Gate working -- see
# PROBE-LIVE-TESTING-GUIDE.md section 8 for why that distinction matters
# for anything beyond demonstrating the mechanics.
#
# adManualGc added after fixing two real bugs in remediator.py's
# _stage1_hybrid() (FUSE's missing _index METADATA column, and the KEEP
# clause omitting fault_class/service) -- both found live via this exact
# flag's own testing. Confirmed twice in a row: memory_hit, confirm
# confidence 0.9, correct_at1=True, correct_at3=True, ~5.5s total
# Remediator+Correlator time once the FUSE fix let stage1 skip its slow
# _search fallback.
WORKING_FAULTS = {"adHighCpu", "adManualGc"}


def set_flag(name: str, variant: str) -> None:
    d = json.loads(FLAGD_PATH.read_text())
    d["flags"][name]["defaultVariant"] = variant
    FLAGD_PATH.write_text(json.dumps(d, indent=2))


def run(flag: str, llm: str | None, minutes: float = 2.0) -> None:
    if flag not in WORKING_FAULTS:
        raise ValueError(f"{flag!r} is not in WORKING_FAULTS ({WORKING_FAULTS}) -- not confirmed working")

    on_variant, target_service = flagd_control.FLAGS[flag]

    confirm_fn = reasoning_fn = None
    if llm == "openai":
        import llm_openai
        confirm_fn, reasoning_fn = llm_openai.confirm, llm_openai.reason
    elif llm == "bedrock":
        import llm_bedrock
        confirm_fn, reasoning_fn = llm_bedrock.confirm, llm_bedrock.reason

    remediator_instance = remediator.Remediator(confirm_fn=confirm_fn) if confirm_fn else remediator.Remediator()
    correlator_instance = correlator.Correlator(reasoning_fn=reasoning_fn) if reasoning_fn else correlator.Correlator()
    streak_state: dict = {}

    set_flag(flag, on_variant)
    plog.emit("harness", "fault_injected", flag=flag, variant=on_variant, target_service=target_service,
               mode="skip_gate")

    try:
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            raw = detector_bridge.scan(streak_state)
            n = len(raw["candidates"]) if raw else 0
            plog.emit("detector", "scan", n_candidates=n, loudest=raw["loudest"] if raw else None)
            if raw:
                tier2 = [c for c in raw["candidates"] if c.tier == 2]
                decision = (Decision(kind="incident", pattern="sustained", trigger=tier2) if tier2
                            else Decision(kind="watch", pattern=None, trigger=[]))
                plog.emit("gate", decision.kind if not tier2 else "bypassed_forced_incident",
                           trigger_service=decision.trigger[0].service if decision.trigger else None)
                if decision.kind == "incident" and any(c.service == target_service for c in decision.trigger):
                    from watch_pipeline import handle_incident
                    handle_incident(decision, raw, remediator_instance, correlator_instance, flag, llm is not None)
                    trig = decision.trigger[0]
                    plog.emit("harness", "incident_handled", flag=flag, trigger_service=trig.service,
                               trigger_signal=trig.signal)
                    return
            time.sleep(POLL_INTERVAL)
        plog.emit("harness", "not_detected", flag=flag, target_service=target_service)
        print(f"No incident touching {target_service!r} within {minutes} minutes.")
    finally:
        set_flag(flag, "off")
        plog.emit("harness", "run_complete", flag=flag)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--flag", default="adHighCpu", choices=sorted(WORKING_FAULTS))
    ap.add_argument("--use-openai", action="store_true")
    ap.add_argument("--use-bedrock", action="store_true")
    ap.add_argument("--minutes", type=float, default=2.0)
    args = ap.parse_args()
    llm = "openai" if args.use_openai else ("bedrock" if args.use_bedrock else None)
    run(args.flag, llm, args.minutes)
