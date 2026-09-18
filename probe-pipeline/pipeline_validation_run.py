"""Validation run: inject one fault flag, poll the real Detector via
detector_bridge, and push whatever it finds through the full
Gate -> Remediator -> Correlator chain. Stub confirm/reasoning_fn unless
BEDROCK_MODEL_ID/AWS_BEARER_TOKEN_BEDROCK or OPENAI_API_KEY are set (see
--llm) -- fail-closed per contract otherwise, still exercises real ES|QL
wiring for stage0/stage1/ruled_out/graph_rank/deepest_span.

Usage:
    python pipeline_validation_run.py                    # adManualGc, default
    python pipeline_validation_run.py --flag cartFailure
    python pipeline_validation_run.py --flag adHighCpu --no-gate-off

--flag defaults to adManualGc, not adHighCpu -- adHighCpu only ever
produces a cpu (resource) signal, which Gate's own contract can never
escalate to an incident alone (see dashboard/README notes). Pick a flag
whose target service (flagd_control.FLAGS) emits a symptom signal
(p95_latency/error_rate) if you want Gate to plausibly open one for real.

GATE_OFF (default on, --no-gate-off to disable): if Gate hasn't reached
"incident" by MAX_WAIT_SECONDS, force a synthetic incident/sustained
Decision from the best candidate seen for the flag's own target service
across the *whole* scan window (not just the final scan -- a real signal
can appear mid-run and fade from the most recent scan by the time the
window ends), falling back to any tier-2 candidate, then anything, only
if the target service never showed up. The real Gate result is recorded
either way, for comparison.
"""
from __future__ import annotations

import argparse
import json
import time

import detector_bridge
import flagd_control
import gate
import remediator
import correlator
import pipeline_log as plog
from schemas import Candidate, Decision, Fingerprint

POLL_SECONDS = 10
MAX_WAIT_SECONDS = 180


def _best_candidate(seen: list[Candidate], target_service: str | None) -> Candidate:
    target_tier2 = [c for c in seen if c.service == target_service and c.tier == 2]
    target_any = [c for c in seen if c.service == target_service]
    tier2 = [c for c in seen if c.tier == 2]
    return (target_tier2 or target_any or tier2 or seen)[0]


def _force_incident(seen: list[Candidate], target_service: str | None) -> Decision:
    best = _best_candidate(seen, target_service)
    return Decision(kind="incident", pattern="sustained", trigger=[best],
                     supporting_evidence=[c for c in seen if c is not best])


def _confirm_fn(llm: str | None):
    if llm == "bedrock":
        import llm_bedrock
        return llm_bedrock.confirm
    if llm == "openai":
        import llm_openai
        return llm_openai.confirm
    from remediator import default_confirm_stub
    return default_confirm_stub


def _reasoning_fn(llm: str | None):
    if llm == "bedrock":
        import llm_bedrock
        return llm_bedrock.reason
    if llm == "openai":
        import llm_openai
        return llm_openai.reason
    from correlator import default_reasoning_stub
    return default_reasoning_stub


def run(flag: str, gate_off: bool = True, llm: str | None = None,
        poll_seconds: int = POLL_SECONDS, max_wait_seconds: int = MAX_WAIT_SECONDS) -> dict:
    if flag not in flagd_control.FLAGS:
        raise ValueError(f"unknown flag {flag!r}; one of {sorted(flagd_control.FLAGS)}")
    on_variant, target_service = flagd_control.FLAGS[flag]

    trace = {"flag": flag, "target_service": target_service, "scans": []}
    flagd_control.set_flag(flag, on_variant)
    trace["injected_at"] = time.time()
    plog.emit("harness", "fault_injected", flag=flag, variant=on_variant, target_service=target_service)

    try:
        streak_state = {}
        g = gate.Gate()
        incident_decision = None
        incident_output = None
        seen_candidates: list[Candidate] = []

        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            output = detector_bridge.scan(streak_state)
            if output:
                seen_candidates.extend(output["candidates"])
            n = len(output["candidates"]) if output else 0
            trace["scans"].append({"t": time.time(), "n_candidates": n,
                                    "loudest": output["loudest"] if output else None})
            plog.emit("detector", "scan", n_candidates=n, loudest=output["loudest"] if output else None)
            decision = g.evaluate(output)
            plog.emit("gate", decision.kind, pattern=decision.pattern,
                       trigger_service=decision.trigger[0].service if decision.trigger else None)
            if decision.kind == "incident":
                incident_decision = decision
                incident_output = output
                break
            time.sleep(poll_seconds)

        trace["gate_result"] = "incident" if incident_decision else "no incident within window"

        if not incident_decision and gate_off and seen_candidates:
            incident_decision = _force_incident(seen_candidates, target_service)
            incident_output = {"candidates": seen_candidates, "loudest": incident_decision.trigger[0].service}
            trig = incident_decision.trigger[0]
            trace["gate_bypassed"] = {"forced_trigger": {"service": trig.service, "signal": trig.signal,
                                                           "tier": trig.tier, "z": trig.z}}
            plog.emit("gate", "bypassed_forced_incident", service=trig.service, signal=trig.signal, z=trig.z)

        if incident_decision:
            trig = incident_decision.trigger[0]
            fp = Fingerprint(
                change_point=trig.type,
                metric=trig.signal,
                loudest_service=incident_output["loudest"],
            )
            symptom = f"{trig.service} {trig.signal} {trig.type}"

            rem = remediator.Remediator(confirm_fn=_confirm_fn(llm))
            rem_out = rem.remediate(incident_decision, fp, symptom)
            trace["remediator"] = {
                "path": rem_out.path,
                "candidates_found": len(rem_out.candidates),
                "timings_ms": rem_out.timings_ms,
            }
            plog.emit("remediator", rem_out.path, candidates_found=len(rem_out.candidates),
                       **{f"{k}_ms": v for k, v in rem_out.timings_ms.items()})

            if rem_out.path == "memory_miss":
                corr = correlator.Correlator(reasoning_fn=_reasoning_fn(llm))
                diagnosis, evidence = corr.correlate(incident_decision, incident_output, rem_out, symptom)
                trace["correlator"] = {
                    "top1_fault_class": diagnosis.top1.fault_class,
                    "top1_service": diagnosis.top1.service,
                    "evidence_keys": sorted(evidence.keys()),
                    "graph_rank": evidence["graph_rank"],
                }
                plog.emit("correlator", "diagnosis", fault_class=diagnosis.top1.fault_class,
                           service=diagnosis.top1.service)
    except Exception as e:
        plog.emit("harness", "error", message=str(e))
        raise
    finally:
        flagd_control.set_flag(flag, "off")
        trace["reset_at"] = time.time()
        plog.emit("harness", "run_complete", flag=flag)

    return trace


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--flag", default="adManualGc", choices=sorted(flagd_control.FLAGS))
    ap.add_argument("--no-gate-off", dest="gate_off", action="store_false")
    ap.add_argument("--llm", choices=["bedrock", "openai"], default=None,
                     help="use a real LLM for confirm/reason instead of the fail-closed stub")
    ap.add_argument("--poll-seconds", type=int, default=POLL_SECONDS)
    ap.add_argument("--max-wait-seconds", type=int, default=MAX_WAIT_SECONDS)
    ap.add_argument("--out", default="pipeline_validation_results.json")
    args = ap.parse_args()

    trace = run(args.flag, gate_off=args.gate_off, llm=args.llm,
                poll_seconds=args.poll_seconds, max_wait_seconds=args.max_wait_seconds)
    json.dump(trace, open(args.out, "w"), indent=2, default=str)
    print(json.dumps(trace, indent=2, default=str))
