"""Deterministic smoke test for gate.py/remediator.py/correlator.py/
grader.py/writer.py, against a faked es_client -- no live Elasticsearch,
no OPENAI_API_KEY, no telemetry required. This is "does the code do
what PROBE-component-contracts.md and PROBE-runbook-search-cases.md
say," not "does it work against your real cluster" -- see
PROBE-PIPELINE-SETUP.md section 3b for the live wiring test that needs
a real stack.

Run: python smoke_test.py
Expect: every line ends "ok", and the last line is
"ALL N CHECKS PASSED". Any AssertionError means a module's behavior no
longer matches its spec -- read the failing check's name, then the
relevant section of PROBE-component-contracts.md /
PROBE-runbook-search-cases.md.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# correlator.py imports detector_bridge.py, which imports v3's
# change_point.py, which resolves its ES auth header at *import time*
# (not lazily on first call -- see PROBE-PIPELINE-SETUP.md section 1b).
# This smoke test never makes a real network call (es_client.esql is
# faked below, and every change_point_fn passed to Correlator here is
# an explicit stub), so a placeholder credential is enough to get past
# that import-time check without needing a real cluster.
os.environ.setdefault("ES_USERNAME", "elastic")
os.environ.setdefault("ES_PASSWORD", "smoke-test-placeholder")

import es_client
import gate
import grader
import remediator
import writer
from schemas import Candidate, Decision, Diagnosis, DiagnosisCandidate, Fingerprint, RemediatorOutput

_checks_passed = 0


def check(name: str, condition: bool) -> None:
    global _checks_passed
    assert condition, f"FAILED: {name}"
    _checks_passed += 1
    print(f"[ok] {name}")


# ======================================================================
# 1. Gate -- contract #2
# ======================================================================

g = gate.Gate()

d1 = g.evaluate({
    "candidates": [Candidate("cart", "p95_latency", "step_change", "t1", 0.001, 5.0, 2)],
    "loudest": "cart", "earliest": "t1", "scan_at": "t1",
})
check("Gate: tier-2 non-recovering symptom -> incident/sustained", d1.kind == "incident" and d1.pattern == "sustained")

g2 = gate.Gate()
for i in range(3):
    d2 = g2.evaluate({
        "candidates": [Candidate("ad", "error_rate", "spike", f"t{i}", 0.01, 4.0, 2)],
        "loudest": "ad", "earliest": f"t{i}", "scan_at": f"t{i}",
    }, now=1000 + i * 10)
check("Gate: third spike on same (service,signal) in 10min -> incident/intermittent", d2.kind == "incident" and d2.pattern == "intermittent")

d3 = g.evaluate({
    "candidates": [Candidate("ad", "cpu", "step_change", "t1", 0.001, 5.0, 2)],
    "loudest": "ad", "earliest": "t1", "scan_at": "t1",
})
check("Gate: resource-only signal never opens an incident alone", d3.kind == "watch")

d4 = g.evaluate({
    "candidates": [
        Candidate("cart", "p95_latency", "step_change", "t1", 0.001, 5.0, 2),
        Candidate("cart", "cpu", "spike", "t1", 0.02, 4.0, 2),
    ],
    "loudest": "cart", "earliest": "t1", "scan_at": "t1",
})
check(
    "Gate: same-scan resource candidate rides along as supporting_evidence",
    [c.signal for c in d4.supporting_evidence] == ["cpu"],
)

# ======================================================================
# 2. Remediator -- search-cases.md cases A, B, C, J + the incident guard
# ======================================================================

fp = Fingerprint(change_point="step_change", metric="p95_latency", loudest_service="cart", dependency="valkey-cart")
incident = Decision(kind="incident", pattern="sustained", trigger=[Candidate("cart", "p95_latency", "step_change", "t1", 0.001, 5.0, 2)])

es_client.esql = lambda q, p=None: []
r = remediator.Remediator()
out_a = r.remediate(incident, fp, "cart latency spike, checkout slow")
check("Remediator Case A: empty memory -> memory_miss, 0 candidates", out_a.path == "memory_miss" and out_a.candidates == [])

runbook_row = {"id": "upstream_dependency_latency__valkey-cart", "status": "candidate", "occurrences": 1,
               "failed_reuses": 0, "root_cause": "x", "steps": [], "symptoms": [], "ruled_out_before": []}
es_client.esql = lambda q, p=None: [runbook_row] if "signature.change_point" in q else []
r_b = remediator.Remediator(confirm_fn=lambda c, f, ro: {"match": True, "confidence": 0.94})
out_b = r_b.remediate(incident, fp, "cart latency spike, checkout slow")
check("Remediator Case B: stage-0 exact hit + confirm -> memory_hit", out_b.path == "memory_hit" and out_b.runbook["id"] == runbook_row["id"])

es_client.get_doc = lambda idx, doc_id: runbook_row
stage0_calls = {"n": 0}
_orig_stage0 = remediator.Remediator._stage0_exact
remediator.Remediator._stage0_exact = staticmethod(lambda fp: (stage0_calls.__setitem__("n", stage0_calls["n"] + 1), [])[1])
out_c = r_b.remediate(incident, fp, "cart latency spike, checkout slow")
remediator.Remediator._stage0_exact = staticmethod(_orig_stage0)
check("Remediator Case C: cache hit skips stage 0/1 entirely", out_c.path == "memory_hit" and stage0_calls["n"] == 0)

es_client.esql = lambda q, p=None: [runbook_row] if "signature.change_point" in q else []
r_j = remediator.Remediator(confirm_fn=lambda c, f, ro: "not a dict")
out_j = r_j.remediate(incident, fp, "symptom")
check("Remediator Case J: malformed confirm response -> memory_miss, never a hit", out_j.path == "memory_miss" and out_j.confirm.get("confirm_error") is True)

try:
    r_b.remediate(Decision(kind="watch", pattern=None, trigger=[]), fp, "x")
    check("Remediator: raises on non-incident Decision", False)
except ValueError:
    check("Remediator: raises on non-incident Decision", True)

# ======================================================================
# 3. Correlator -- contract #4's memory_miss-only guard + evidence shape
# ======================================================================

import correlator  # noqa: E402  (imported here, after ES creds may not exist -- see note below)

det_out = {"candidates": [Candidate("cart", "p95_latency", "step_change", "t1", 0.001, 5.0, 2)], "loudest": "cart", "earliest": "t1", "scan_at": "t1"}
rem_out_miss = RemediatorOutput("memory_miss", None, [], [], {"match": False, "confidence": 0.0}, {})
rem_out_hit = RemediatorOutput("memory_hit", {"id": "x"}, [], [], {"match": True, "confidence": 0.9}, {})

es_client.esql = lambda q, p=None: []
c = correlator.Correlator(service_graph_path=Path("/nonexistent.json"), change_point_fn=lambda s, sig: None)
diag, evidence = c.correlate(incident, det_out, rem_out_miss, "cart latency spike, checkout slow")
check("Correlator: runs on memory_miss, all 8 evidence keys present", sorted(evidence.keys()) == sorted(
    ["graph_rank", "deepest_span", "correlated", "recent_changes", "extra_hops", "supporting", "near_matches", "ruled_out"]
))
check("Correlator: unwired reasoning_fn fails closed to unknown", diag.top1.fault_class == "unknown")

try:
    c.correlate(incident, det_out, rem_out_hit, "x")
    check("Correlator: raises on memory_hit input", False)
except ValueError:
    check("Correlator: raises on memory_hit input", True)

# ======================================================================
# 4. Grader -- contract #5, no LLM, no ES
# ======================================================================

diag_correct = Diagnosis(candidates=[DiagnosisCandidate("upstream_dependency_latency", "valkey-cart", 0.9)], root_cause="x", symptom="s", steps=[], source="correlator")
run_correct = grader.grade_incident(diag_correct, rem_out_miss, "upstream_dependency_latency", "valkey-cart")
check("Grader: fault_class+service both match -> correct_at1", run_correct.correct_at1 is True)

run_wrong = grader.grade_incident(diag_correct, rem_out_miss, "resource_exhaustion", "cart")
check("Grader: mismatch on either field -> not correct_at1", run_wrong.correct_at1 is False)

check("Grader: null window, Gate said watch -> correct", grader.grade_null_window("watch").correct_at1 is True)
check("Grader: null window, Gate opened an incident -> false positive", grader.grade_null_window("incident").correct_at1 is False)

# ======================================================================
# 5. Writer -- contract #6
# ======================================================================

store: dict[str, dict] = {}
es_client.get_doc = lambda idx, doc_id: store.get(doc_id)
es_client.index_doc = lambda idx, doc_id, body: store.update({doc_id: dict(body)})
es_client.update_doc = lambda idx, doc_id, patch: store[doc_id].update(patch)
es_client.esql = lambda q, p=None: []

rem_for_writer = remediator.Remediator()
w = writer.Writer(rem_for_writer)

res1 = w.write(run_correct, diag_correct, fp, "run_001", "cart latency spike")
check("Writer: correct grade creates a runbook", res1["action"] == "upsert_runbook" and store[res1["runbook_id"]]["occurrences"] == 1)

# Simulate the fingerprint cache already pointing at this runbook
# *before* the next write, so we can check the write clears it.
rem_for_writer._cache_set(fp, res1["runbook_id"])
check("Writer: cache pre-condition set correctly", rem_for_writer._cache_get(fp) == res1["runbook_id"])

res2 = w.write(run_correct, diag_correct, fp, "run_002", "cart latency spike, checkout slow")
check("Writer: correct grade again -> occurrences++, symptom appended, semantic widened",
      store[res1["runbook_id"]]["occurrences"] == 2
      and "cart latency spike, checkout slow" in store[res1["runbook_id"]]["symptoms"]
      and store[res1["runbook_id"]]["semantic"] == "\n".join(store[res1["runbook_id"]]["symptoms"]))
check("Writer: that write called remediator.invalidate(), clearing the cache", rem_for_writer._cache_get(fp) is None)

run_wrong_hit = grader.grade_incident(
    diag_correct,
    RemediatorOutput("memory_hit", dict(store[res1["runbook_id"]], id=res1["runbook_id"]), [], [], {"match": True, "confidence": 0.9}, {}),
    "resource_exhaustion", "cart",  # actual truth differs -- this hit was wrong
)
res3 = w.write(run_wrong_hit, diag_correct, fp, "run_019", "s")
check("Writer: wrong + memory_hit -> demotes the reused runbook", store[res1["runbook_id"]]["status"] == "demoted" and store[res1["runbook_id"]]["failed_reuses"] == 1)
check("Writer: the TRUE answer is never written anywhere", not any("resource_exhaustion" in str(v) for v in store.values()))

diag_abstain = Diagnosis(candidates=[DiagnosisCandidate("unknown", "x", 0.0)], root_cause="", symptom="s", steps=[], source="correlator")
run_abstain = grader.grade_incident(diag_abstain, rem_out_miss, "resource_exhaustion", "cart")
before = dict(store)
w.write(run_abstain, diag_abstain, fp, "run_020", "s")
check("Writer: abstained -> writes nothing", store == before)

run_ablation = grader.grade_incident(diag_correct, rem_out_hit, "resource_exhaustion", "cart", config="no_remediator")
before2 = dict(store)
w.write(run_ablation, diag_correct, fp, "run_021", "s")
check("Writer: ablation run -> never touches memory", store == before2)

print(f"\nALL {_checks_passed} CHECKS PASSED")
