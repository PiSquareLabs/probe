"""Same comparison as compare_token_usage.py -- a fault's first, cold
occurrence (Correlator's one reasoning call) vs. its second occurrence
(stage-0 exact hit, confirm call only, Correlator skipped) -- but
against the REAL OpenAI API (gpt-4o-mini, llm_openai.py's own MODEL)
instead of a char-count estimate. Needs OPENAI_API_KEY in the
environment; makes real, billed API calls (a handful of small ones).

es_client is still faked (no live Elasticsearch needed -- same
store-backed style as smoke_test.py/compare_token_usage.py), and the
symptom/evidence fed to the real reasoning call is grounded in
catalog/cartFailure.yaml -- a real, reviewed flag write-up -- rather
than made-up text, per the ask to find the runbook from the catalog
instead of a synthetic one.

Run: OPENAI_API_KEY=sk-... python compare_token_usage_live.py
Expect: a comparison table of REAL token counts, then "PASSED".
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if not os.environ.get("OPENAI_API_KEY"):
    print("OPENAI_API_KEY not set -- export it first (see this file's docstring).")
    sys.exit(1)

os.environ.setdefault("ES_USERNAME", "elastic")
os.environ.setdefault("ES_PASSWORD", "compare-test-placeholder")

import catalog_runbook
import es_client
import grader
import llm_openai
import writer
from correlator import Correlator
from remediator import Remediator
from schemas import Candidate, Decision, Fingerprint

# -- ground the scenario in a real, reviewed catalog entry ---------------
cart_flag = catalog_runbook.read_by_flag("cartFailure")
if cart_flag is None:
    print("catalog/cartFailure.yaml not found or unreadable -- run this from probe-pipeline/.")
    sys.exit(1)

# -- fake es_client, same store-backed style as smoke_test.py -----------
store: dict[str, dict] = {}
es_client.get_doc = lambda idx, doc_id: store.get(doc_id)
es_client.index_doc = lambda idx, doc_id, body: store.update({doc_id: dict(body)})
es_client.update_doc = lambda idx, doc_id, patch: store[doc_id].update(patch)


def fake_esql(query: str, params: dict | None = None) -> list[dict]:
    if "kind == \"ruled_out\"" in query:
        return []
    if "signature.change_point" in query and "FUSE" not in query:
        for doc in store.values():
            if doc.get("kind") != "runbook" or doc.get("status") == "demoted":
                continue
            sig = doc.get("signature", {})
            if (
                sig.get("change_point") == params["change_point"]
                and sig.get("metric") == params["metric"]
                and sig.get("loudest_service") == params["loudest"]
                and sig.get("dependency") == params["dependency"]
            ):
                return [{**doc, "id": next(k for k, v in store.items() if v is doc)}]
        return []
    return []  # stage 1 / correlator's own ES|QL reads: nothing relevant in this fixture


es_client.esql = fake_esql

fp = Fingerprint(
    change_point="spike",
    metric="error_rate",
    loudest_service="checkout",
    # catalog/cartFailure.yaml's own expected_signature.dependency --
    # CatalogRunbook doesn't carry that field, so it's copied here rather
    # than re-parsing the yaml a second time.
    dependency="valkey/redis (hardcoded to unreachable badhost:1234)",
)
incident = Decision(kind="incident", pattern="sustained", trigger=[Candidate("checkout", "error_rate", "spike", "t1", 0.001, 5.0, 2)])
symptom = f"{cart_flag.service}'s EmptyCart RPC failing, {fp.loudest_service} seeing downstream errors"
det_out = {"candidates": [Candidate("checkout", "error_rate", "spike", "t1", 0.001, 5.0, 2)], "loudest": "checkout", "earliest": "t1", "scan_at": "t1"}

remediator = Remediator(confirm_fn=llm_openai.confirm)
correlator = Correlator(service_graph_path=Path("/nonexistent.json"), change_point_fn=lambda s, sig: None, reasoning_fn=llm_openai.reason)
w = writer.Writer(remediator)


def run_incident(run_id: str) -> dict:
    rem_out = remediator.remediate(incident, fp, symptom)
    reasoning_calls = 0
    diagnosis_tokens = 0
    if rem_out.path == "memory_miss":
        diagnosis, _evidence = correlator.correlate(incident, det_out, rem_out, symptom)
        reasoning_calls = 1
        diagnosis_tokens = diagnosis.tokens
        probe_run = grader.grade_incident(diagnosis, rem_out, diagnosis.top1.fault_class, diagnosis.top1.service)
        w.write(probe_run, diagnosis, fp, run_id, symptom)
        print(f"  [{run_id}] Correlator answered: {diagnosis.top1.fault_class}/{diagnosis.top1.service} -- {diagnosis.root_cause[:80]}")
    else:
        print(f"  [{run_id}] confirm on cached candidate: {rem_out.confirm}")
    total_tokens = rem_out.tokens + diagnosis_tokens
    return {
        "path": rem_out.path,
        "confirm_calls": 1 if rem_out.tokens > 0 else 0,
        "reasoning_calls": reasoning_calls,
        "confirm_tokens": rem_out.tokens,
        "reasoning_tokens": diagnosis_tokens,
        "total_tokens": total_tokens,
    }


print(f"Real root cause (catalog/cartFailure.yaml): {cart_flag.cause}\n")
print("Running incident 1 (cold)...")
first = run_incident("run_001")
print("Running incident 2 (same fingerprint, recurrence)...")
second = run_incident("run_002")

print(f"\n{'':28}{'1st occurrence (cold)':>24}{'2nd occurrence (recurrence)':>30}")
print(f"{'path':28}{first['path']:>24}{second['path']:>30}")
print(f"{'confirm (stage-2) calls':28}{first['confirm_calls']:>24}{second['confirm_calls']:>30}")
print(f"{'reasoning (Correlator) calls':28}{first['reasoning_calls']:>24}{second['reasoning_calls']:>30}")
print(f"{'confirm tokens (real)':28}{first['confirm_tokens']:>24}{second['confirm_tokens']:>30}")
print(f"{'reasoning tokens (real)':28}{first['reasoning_tokens']:>24}{second['reasoning_tokens']:>30}")
print(f"{'TOTAL tokens (real)':28}{first['total_tokens']:>24}{second['total_tokens']:>30}")

saved = first["total_tokens"] - second["total_tokens"]
pct = (saved / first["total_tokens"] * 100) if first["total_tokens"] else 0.0
print(f"\nTokens saved on recurrence: {saved} ({pct:.0f}% less than the cold run)")

assert first["path"] == "memory_miss", f"expected the first occurrence to be a cold memory_miss, got {first['path']}"
assert second["path"] == "memory_hit", f"expected the second occurrence to be a memory_hit, got {second['path']} (confirm={second})"
assert second["reasoning_calls"] == 0, "the recurrence must never re-run the Correlator's reasoning call"
assert second["total_tokens"] < first["total_tokens"], "the recurrence must use strictly fewer tokens than the cold run"

print("\nPASSED")
