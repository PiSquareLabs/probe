"""Clears one flag's scoped test state from probe-memory before a
Technical Test run: the runbook doc for that flag's (fault_class,
service) key, and any ruled_out docs proposing that same key. Scoped,
not a full-index wipe -- the dashboard's Technical Test tab calls this
so a repeated cold/warm test doesn't inherit a runbook from a previous
session, without touching unrelated real data other flags may have
written.

Note: adHighCpu and adManualGc key to the SAME (resource_exhaustion,
ad) runbook (catalog_runbook.py's own documented collision) -- clearing
either one clears that shared runbook.

Run against the real cluster via serve_dashboard.py's
POST /api/techtest/clear, which launches this as a subprocess so
ES_URL/ES_API_KEY typed into the dashboard's Settings tab reach it via
env, same convention as /api/run -> run_working_fault.py.

Usage:
    python techtest_clear.py --flag adHighCpu
Prints a JSON summary to stdout; a non-zero exit code means it failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog_runbook
import es_client
from writer import Writer


def clear(flag: str) -> dict:
    catalog_entry = catalog_runbook.read_by_flag(flag)
    if catalog_entry is None:
        raise ValueError(f"no catalog/{flag}.yaml entry -- can't resolve (fault_class, service) to clear")

    fault_class, service = catalog_entry.fault_class, catalog_entry.service
    runbook_id = Writer._runbook_id(fault_class, service)

    existed = es_client.get_doc("probe-memory", runbook_id) is not None
    es_client.delete_doc("probe-memory", runbook_id)

    ruled_out_query = """
        FROM probe-memory METADATA _id
        | WHERE kind == "ruled_out" AND proposed.fault_class == ?fault_class AND proposed.service == ?service
        | KEEP _id
    """
    ruled_out_ids = [
        r["_id"] for r in es_client.esql(ruled_out_query, {"fault_class": fault_class, "service": service})
    ]
    for doc_id in ruled_out_ids:
        es_client.delete_doc("probe-memory", doc_id)

    return {
        "cleared": True,
        "flag": flag,
        "fault_class": fault_class,
        "service": service,
        "runbook_id": runbook_id,
        "runbook_existed": existed,
        "ruled_out_deleted": len(ruled_out_ids),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--flag", required=True)
    args = ap.parse_args()
    try:
        print(json.dumps(clear(args.flag)))
    except Exception as e:
        print(json.dumps({"cleared": False, "error": str(e)}))
        sys.exit(1)
