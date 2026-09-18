"""Records a developer's verdict on a Technical Test run's diagnosis:
--verdict correct marks the matched/written runbook verified (optionally
with edited root_cause/steps that more closely match the actual signal)
and provisions that service's Agent Builder candidate-query tool
(Writer.mark_dev_verified -- same mechanism as a Jira Fixed confirmation,
triggered by the dashboard's feedback popup instead of a ticket close).
--verdict incorrect writes a ruled_out record (Writer.mark_jira_rejected).

Run against the real cluster via serve_dashboard.py's
POST /api/techtest/feedback, which launches this as a subprocess so
ES_URL/ES_API_KEY/KIBANA_URL/KIBANA_API_KEY typed into the dashboard's
Settings tab reach it via env, same convention as /api/run.

Usage:
    python techtest_feedback.py --runbook-id upstream_dependency_error__cart \\
        --verdict correct [--root-cause "..."] [--steps-json '["step 1", "step 2"]']

    python techtest_feedback.py --verdict incorrect \\
        --fault-class resource_exhaustion --service ad \\
        --incident-id run_abc123 --symptom "ad cpu spike, loudest ad"

Prints a JSON result to stdout; a non-zero exit code means it failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from remediator import Remediator
from writer import Writer


def feedback(args: argparse.Namespace) -> dict:
    w = Writer(Remediator())

    if args.verdict == "correct":
        if not args.runbook_id:
            raise ValueError("--verdict correct requires --runbook-id")
        edited_steps = json.loads(args.steps_json) if args.steps_json else None
        return w.mark_dev_verified(args.runbook_id, edited_root_cause=args.root_cause, edited_steps=edited_steps)

    if args.verdict == "incorrect":
        missing = [f for f in ("fault_class", "service", "incident_id", "symptom") if not getattr(args, f)]
        if missing:
            raise ValueError(f"--verdict incorrect requires --{', --'.join(m.replace('_', '-') for m in missing)}")
        w.mark_jira_rejected(args.fault_class, args.service, args.incident_id, args.symptom)
        return {"rejected": True, "fault_class": args.fault_class, "service": args.service}

    raise ValueError(f"unknown --verdict {args.verdict!r}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdict", required=True, choices=["correct", "incorrect"])
    ap.add_argument("--runbook-id")
    ap.add_argument("--root-cause")
    ap.add_argument("--steps-json")
    ap.add_argument("--fault-class")
    ap.add_argument("--service")
    ap.add_argument("--incident-id")
    ap.add_argument("--symptom")
    parsed = ap.parse_args()
    try:
        print(json.dumps(feedback(parsed)))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
