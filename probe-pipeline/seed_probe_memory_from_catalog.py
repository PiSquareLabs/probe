"""One-off: seed probe-memory with a real runbook per catalog/*.yaml
entry, so Remediator's stage 0/1 have something real to match against
instead of an empty index. Mirrors writer.py's _upsert_runbook()
"existing is None" branch exactly -- same document shape, same id
scheme (f"{fault_class}__{service}") -- but driven by catalog content
instead of a graded pipeline run, since there is no real Diagnosis to
seed from yet (chicken-and-egg on a fresh install).

Known, documented collision (PROBE-PIPELINE-SETUP.md §4): adHighCpu and
adManualGc both key to resource_exhaustion__ad. This script does not
work around that -- the second write naturally merges into the first's
runbook via the same upsert logic writer.py itself uses (append symptom,
increment occurrences), which is exactly what would happen in a real
graded run too. Left as-is on purpose to demonstrate the real behavior.

Run: python seed_probe_memory_from_catalog.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog_runbook
import es_client

RUNBOOK_ID = "{}__{}".format


def seed() -> None:
    written, merged = 0, 0
    for rb in catalog_runbook.all_runbooks():
        runbook_id = RUNBOOK_ID(rb.fault_class, rb.service)
        existing = es_client.get_doc("probe-memory", runbook_id)
        symptom = f"{rb.flag}: {rb.cause}"

        if existing is None:
            es_client.index_doc(
                "probe-memory",
                runbook_id,
                {
                    "kind": "runbook",
                    "fault_class": rb.fault_class,
                    "service": rb.service,
                    "status": "candidate",
                    "occurrences": 1,
                    "failed_reuses": 0,
                    "verified_by": [],
                    "root_cause": rb.cause,
                    "steps": rb.steps + [rb.permanent_fix],
                    "symptoms": [symptom],
                    "runs": [f"seed__{rb.flag}"],
                    "ruled_out_before": [],
                    "signature": {
                        "change_point": "step_change",
                        "metric": "p95_latency",
                        "loudest_service": rb.service,
                        "dependency": None,
                    },
                    "semantic": symptom,
                },
            )
            print(f"[written] {runbook_id}  <- {rb.flag}")
            written += 1
        else:
            symptoms = list(existing.get("symptoms", []))
            if symptom not in symptoms:
                symptoms.append(symptom)
            runs = list(existing.get("runs", [])) + [f"seed__{rb.flag}"]
            es_client.update_doc(
                "probe-memory",
                runbook_id,
                {
                    "occurrences": existing.get("occurrences", 0) + 1,
                    "symptoms": symptoms,
                    "semantic": "\n".join(symptoms),
                    "runs": runs,
                },
            )
            print(f"[merged]  {runbook_id}  <- {rb.flag} (collision with an existing runbook)")
            merged += 1

    print(f"\n{written} runbooks written, {merged} merged into an existing runbook (collisions).")


if __name__ == "__main__":
    seed()
