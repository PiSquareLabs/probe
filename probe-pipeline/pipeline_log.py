"""Structured event log for the pipeline harness, consumed by
dashboard.html via serve_dashboard.py.

Not a replacement for probe-runs (the Grader's own Elasticsearch
record) -- this is a local, append-only JSONL trace of what each stage
did on each run, for a human watching the dashboard while a harness
script (pipeline_validation_run.py, or any future orchestrator loop)
executes. One line per event, oldest first, never rewritten.

Usage, from any pipeline stage or harness script:

    import pipeline_log as plog
    plog.emit("detector", "scan", n_candidates=3, loudest="cart")
    plog.emit("gate", "incident", pattern="sustained", trigger_service="cart")
    plog.emit("remediator", "memory_miss", candidates_found=3, stage0_ms=127)
    plog.emit("correlator", "diagnosis", fault_class="unknown", service="recommendation")

Each call appends one JSON object with a server-assigned `ts` (unix
epoch, seconds) and `run_id` (shared per Python process so the
dashboard can group events from the same execution) to
pipeline_events.jsonl in this directory. No Elasticsearch dependency --
this only needs local disk, so it works even when ES_URL/ES_API_KEY
aren't set.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "pipeline_events.jsonl"

# One id per process -- every emit() call in this run shares it, so the
# dashboard can group/color events by execution without the caller
# having to thread a run_id through every function signature.
RUN_ID = os.environ.get("PIPELINE_RUN_ID") or uuid.uuid4().hex[:8]


def emit(stage: str, event: str, **fields) -> None:
    """Append one event. stage is one of detector/gate/remediator/
    correlator/grader/writer/harness (free-form, dashboard.html doesn't
    hardcode the set). event is a short label (e.g. "scan", "incident",
    "memory_hit", "error"). fields is whatever's useful to show -- kept
    as opaque JSON, not a fixed schema, since each stage's interesting
    fields differ (Gate's decision.kind, Remediator's timings_ms, ...).
    """
    record = {"ts": time.time(), "run_id": RUN_ID, "stage": stage, "event": event, **fields}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def read_all() -> list[dict]:
    """Used by serve_dashboard.py and by tests -- not needed by emit()
    callers. Tolerates a missing file (nothing logged yet) and skips
    any line that fails to parse rather than raising, so one corrupted
    line (e.g. a write torn by a killed process) doesn't take down the
    whole dashboard.
    """
    if not LOG_PATH.exists():
        return []
    records = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def clear() -> None:
    """For starting a fresh dashboard view before a new demo run --
    never called automatically, only on explicit request (e.g. a
    `python pipeline_log.py --clear` invocation), since the log is
    meant to accumulate across runs by default."""
    LOG_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    import sys
    if "--clear" in sys.argv:
        clear()
        print(f"cleared {LOG_PATH}")
    else:
        records = read_all()
        print(f"{len(records)} events in {LOG_PATH}")
        for r in records[-10:]:
            print(json.dumps(r))
