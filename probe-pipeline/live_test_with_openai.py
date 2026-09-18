"""Standalone live test: real Detector (v3) -> Gate -> Remediator (real
confirm via gpt-4o-mini) against the real seeded probe-memory. Starts
from an actual detector_bridge.scan() -- the real Tier 1 z-score +
Tier 2 CHANGE_POINT pipeline reading live telemetry -- not a synthetic
candidate. Polls until it sees a real incident or times out.

Run this yourself in a terminal where OPENAI_API_KEY, ES_URL, and
ES_API_KEY are all set as environment variables -- see
RUNNING_ON_ELASTIC_CLOUD.md §3 for the ES ones.

    $env:ES_URL = "https://my-observability-project-b14780.es.asia-south1.gcp.elastic.cloud:443"
    $env:ES_API_KEY = "<your query API key>"
    $env:OPENAI_API_KEY = "sk-proj-..."
    cd probe-pipeline
    python live_test_with_openai.py

A fault needs to actually be injected in the demo app (via
demo.flagd.json) for this to see anything other than "no candidates" --
confirm one is active before running this, or it'll just poll quietly
until MAX_WAIT_SECONDS.

Prints every step to the terminal AND writes the full result to
live_test_result.json next to this script, so it can be inspected
afterward without needing API access.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import detector_bridge
import gate
import llm_openai
import remediator
from schemas import Decision, Fingerprint

POLL_INTERVAL = 10
MAX_WAIT_SECONDS = 180

# DIAGNOSTIC-ONLY, per explicit request: skip Gate's incident/watch/
# transient classification entirely and force whatever the Detector
# finds straight into Remediator, so the confirm/retrieval wiring can be
# exercised without waiting for a candidate that also happens to satisfy
# Gate's rules (tier-2 symptom signal, non-recovering). Never do this on
# the graded path -- Gate existing is the whole point of not paging on
# every resource blip.
BYPASS_GATE = True


def main() -> None:
    streak_state: dict = {}
    g = gate.Gate()
    rem = remediator.Remediator(confirm_fn=llm_openai.confirm)

    print(f"Polling the real Detector every {POLL_INTERVAL}s for up to {MAX_WAIT_SECONDS}s...")
    if BYPASS_GATE:
        print("BYPASS_GATE=True -- Gate's incident/watch/transient filtering is disabled for this run.")
    deadline = time.time() + MAX_WAIT_SECONDS
    decision = None
    raw = None

    while time.time() < deadline:
        # This remote project's load-generator produces less traffic to
        # `ad` per 10s bucket than the local Docker setup this floor was
        # tuned against (most buckets had <20 spans, silently dropped) --
        # lowered for this test only, not a change to the real detector's
        # defaults.
        raw = detector_bridge.scan(streak_state, min_spans_per_bucket=5)
        now = datetime.now(timezone.utc).isoformat()
        if raw is None:
            print(f"[{now}] Detector: no candidates")
        else:
            print(f"[{now}] Detector: {len(raw['candidates'])} candidate(s), loudest={raw['loudest']}")
            real_decision = g.evaluate(raw)
            print(f"    Gate (real): {real_decision.kind} {real_decision.pattern}")
            if BYPASS_GATE:
                # Prefer a tier-2-confirmed candidate; fall back to
                # whichever the Detector called loudest.
                chosen = next((c for c in raw["candidates"] if c.tier == 2), None) or next(
                    c for c in raw["candidates"] if c.service == raw["loudest"]
                )
                decision = Decision(kind="incident", pattern="sustained", trigger=[chosen])
                print(f"    Gate (bypassed): forcing incident on {chosen.service}/{chosen.signal} (tier {chosen.tier})")
                break
            decision = real_decision
            if decision.kind == "incident":
                break
        time.sleep(POLL_INTERVAL)

    if decision is None or decision.kind != "incident":
        print("\nNo real incident seen within the time budget -- nothing to remediate.")
        print("Make sure a fault is actually injected in the demo app, then re-run.")
        return

    trigger = decision.trigger[0]
    fp = Fingerprint(
        change_point=trigger.type,
        metric=trigger.signal,
        loudest_service=raw["loudest"],
        dependency=None,
    )
    symptom = f"{raw['loudest']} {trigger.signal} anomaly, tier {trigger.tier}"
    print(f"\nReal incident detected. Fingerprint: {fp}")
    print(f"Symptom: {symptom!r}")

    rem_out = rem.remediate(decision, fp, symptom=symptom)
    print(f"\nRemediator path: {rem_out.path}")
    print(f"Candidates found: {[c.get('id') for c in rem_out.candidates]}")
    print(f"Timings (ms): {rem_out.timings_ms}")
    print(f"REAL confirm result from gpt-4o-mini: {rem_out.confirm}")
    if rem_out.runbook:
        print(f"Matched runbook id: {rem_out.runbook.get('id')}")
        print(f"Matched runbook root_cause: {rem_out.runbook.get('root_cause')}")
        print(f"Matched runbook steps: {rem_out.runbook.get('steps')}")

    result = {
        "gate_bypassed": BYPASS_GATE,
        "detector_raw": {
            "candidates": [
                {"service": c.service, "signal": c.signal, "type": c.type, "z": c.z, "tier": c.tier}
                for c in raw["candidates"]
            ],
            "loudest": raw["loudest"],
            "earliest": raw["earliest"],
        },
        "gate_decision": {"kind": decision.kind, "pattern": decision.pattern},
        "fingerprint": {"change_point": fp.change_point, "metric": fp.metric, "loudest_service": fp.loudest_service},
        "remediator_path": rem_out.path,
        "candidates_considered": [c.get("id") for c in rem_out.candidates],
        "timings_ms": rem_out.timings_ms,
        "confirm_result": rem_out.confirm,
        "matched_runbook": rem_out.runbook,
    }
    out_path = Path(__file__).resolve().parent / "live_test_result.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"\nWrote full result to {out_path}")


if __name__ == "__main__":
    main()
