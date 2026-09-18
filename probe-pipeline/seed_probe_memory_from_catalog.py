"""Seeds `probe-memory` with a runbook doc per catalog/*.yaml entry, using
each entry's `truth`/`expected_signature`/`reference_runbook` fields --
so Remediator's stage 0 (exact signature match) and stage 1 (hybrid
keyword+semantic search) have real, correct answers to retrieve instead
of an empty index. Writes the same document shape writer.py's
_upsert_runbook() produces (see that file), with status "verified" and
verified_by ["catalog_seed"] to mark these as curated ground truth, not
LLM-written candidates.

Requires the `probe-memory` index to already exist -- run
setup_probe_memory.py first if `python seed_probe_memory_from_catalog.py`
fails with an index-not-found error.

The (fault_class, service) collision documented in
PROBE-LIVE-TESTING-GUIDE.md section 9 (adHighCpu and adManualGc both key
to resource_exhaustion__ad) is handled here: the second colliding entry
is seeded under a distinct id (resource_exhaustion__ad__adManualGc)
rather than silently overwriting the first, since writer.py's own
_runbook_id() does not do this automatically.

Usage:
    export ES_URL=... ES_API_KEY=...   # or ES_USERNAME/ES_PASSWORD for local
    python setup_probe_memory.py        # once, if not already done
    python seed_probe_memory_from_catalog.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

import es_client

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = REPO_ROOT / "catalog"
INDEX_NAME = "probe-memory"


def _runbook_id(fault_class: str, service: str) -> str:
    return f"{fault_class}__{service}"


def main() -> None:
    seen_ids: dict[str, str] = {}  # runbook_id -> flag that first claimed it
    seeded, skipped = [], []

    for path in sorted(CATALOG_DIR.glob("*.yaml")):
        entry = yaml.safe_load(path.read_text())
        flag = entry["flag"]
        truth = entry["truth"]
        sig = entry["expected_signature"]
        rb = entry["reference_runbook"]

        fault_class, service = truth["fault_class"], truth["service"]
        runbook_id = _runbook_id(fault_class, service)
        if runbook_id in seen_ids:
            # collision (see module docstring) -- seed under a distinct id
            # rather than overwrite the flag that got there first
            runbook_id = f"{runbook_id}__{flag}"
        seen_ids[runbook_id] = flag

        symptom = f"{flag} injected on {service}"
        body = {
            "kind": "runbook",
            "fault_class": fault_class,
            "service": service,
            "status": "verified",
            "occurrences": 0,
            "failed_reuses": 0,
            "verified_by": ["catalog_seed"],
            "root_cause": rb["cause"],
            "steps": rb["steps"],
            "symptoms": [symptom],
            "runs": [],
            "ruled_out_before": [],
            "signature": {
                "change_point": sig.get("change_point"),
                "metric": sig.get("metric"),
                "loudest_service": sig.get("loudest_service"),
                "dependency": sig.get("dependency"),
            },
            "semantic": f"{rb['cause']} {rb.get('permanent_fix', '')}".strip(),
        }
        es_client.index_doc(INDEX_NAME, runbook_id, body)
        seeded.append((runbook_id, flag))
        print(f"seeded {runbook_id:45s} <- {flag}")

    print(f"\n{len(seeded)} runbook(s) seeded into {INDEX_NAME}.")
    if skipped:
        print(f"{len(skipped)} skipped: {skipped}")


if __name__ == "__main__":
    main()
