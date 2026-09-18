"""Token-usage comparison: a fault's first occurrence (cold memory --
stage 0/1 both empty, Correlator runs its one reasoning call) against
the *same* fault's second occurrence (stage-0 exact hit, confirm only,
Correlator never runs). PROBE-runbook-search-cases.md's summary table
already claims the second is faster in wall-clock terms (~20s -> ~3s,
Case A -> Case B); this is the token-cost side of that same claim,
which nothing in this repo measured before -- llm_openai.py's _chat()
used to throw away OpenAI's own `usage.total_tokens` entirely, so
ProbeRun.tokens (schemas.py) had a field with nothing real ever put in
it (see the tokens plumbing added to schemas.py/llm_openai.py/
remediator.py/correlator.py alongside this script).

No live OpenAI or Elasticsearch needed. es_client is faked the same
way smoke_test.py fakes it (module functions replaced with a tiny
in-memory dict store). llm_openai._chat is faked too, but confirm()/
reason() themselves are NOT -- their real prompt-building code runs
unchanged, so the token counts below come from the actual system+user
strings this project would send, sized with a standard ~4-chars-per-
token estimate (https://platform.openai.com/tokenizer's own rule of
thumb; close enough to compare two prompts built by the same estimator,
which is all a same-model comparison needs), not two made-up constants.

Run: python compare_token_usage.py
Expect: a comparison table, then "PASSED" -- the second occurrence
must use strictly fewer tokens and must make zero reasoning calls.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("ES_USERNAME", "elastic")
os.environ.setdefault("ES_PASSWORD", "compare-test-placeholder")
os.environ["OPENAI_API_KEY"] = "test-placeholder-key"  # never actually sent; _chat is faked below

import es_client
import grader
import llm_openai
import writer
from correlator import Correlator
from remediator import Remediator
from schemas import Candidate, Decision, Fingerprint

CHARS_PER_TOKEN = 4  # OpenAI's own rule-of-thumb estimator


def fake_chat(system: str, user: str, max_tokens: int) -> tuple[str, int]:
    """Stands in for the real OpenAI call: returns a valid response for
    whichever of the two real system prompts is asking (told apart by
    their actual, distinct wording -- not a call-order guess), and a
    token count estimated from the REAL prompt text llm_openai.py built,
    plus the completion length the caller actually asked for.
    """
    prompt_tokens = (len(system) + len(user)) // CHARS_PER_TOKEN
    if "confirm step" in system:
        completion = '{"match": true, "confidence": 0.94}'
    elif "root-cause reasoning step" in system:
        completion = (
            '{"candidates": [{"fault_class": "upstream_dependency_latency", '
            '"service": "cart", "confidence": 0.8}], '
            '"root_cause": "valkey-cart connection pool exhausted under load", '
            '"steps": ["check valkey-cart pool metrics", "bump max connections"]}'
        )
    else:
        raise AssertionError(f"fake_chat doesn't recognize this system prompt: {system[:60]!r}")
    completion_tokens = len(completion) // CHARS_PER_TOKEN
    return completion, prompt_tokens + completion_tokens


llm_openai._chat = fake_chat

# -- fake es_client, same store-backed style as smoke_test.py -----------
store: dict[str, dict] = {}
es_client.get_doc = lambda idx, doc_id: store.get(doc_id)
es_client.index_doc = lambda idx, doc_id, body: store.update({doc_id: dict(body)})
es_client.update_doc = lambda idx, doc_id, patch: store[doc_id].update(patch)


def fake_esql(query: str, params: dict | None = None) -> list[dict]:
    if "kind == \"ruled_out\"" in query:
        return []
    if "signature.change_point" in query and "FUSE" not in query:
        # stage 0: exact match iff a runbook with this exact signature exists
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

fp = Fingerprint(change_point="step_change", metric="p95_latency", loudest_service="cart", dependency="valkey-cart")
incident = Decision(kind="incident", pattern="sustained", trigger=[Candidate("cart", "p95_latency", "step_change", "t1", 0.001, 5.0, 2)])
symptom = "cart latency spike, checkout slow"
det_out = {"candidates": [Candidate("cart", "p95_latency", "step_change", "t1", 0.001, 5.0, 2)], "loudest": "cart", "earliest": "t1", "scan_at": "t1"}

remediator = Remediator(confirm_fn=llm_openai.confirm)
correlator = Correlator(service_graph_path=Path("/nonexistent.json"), change_point_fn=lambda s, sig: None, reasoning_fn=llm_openai.reason)
w = writer.Writer(remediator)


def run_incident(run_id: str) -> dict:
    rem_out = remediator.remediate(incident, fp, symptom)
    reasoning_calls = 0
    diagnosis_tokens = 0
    if rem_out.path == "memory_miss":
        # Cold path: Correlator runs its one reasoning call, and its
        # answer is what the Writer persists as the runbook run 2's
        # stage-0 exact match needs to find. (Nothing in this repo wires
        # a real orchestrator that builds a Diagnosis from a *hit* --
        # see correlator.py's own note that no orchestrator exists yet --
        # so a hit here is graded and reported on, not re-written.)
        diagnosis, _evidence = correlator.correlate(incident, det_out, rem_out, symptom)
        reasoning_calls = 1
        diagnosis_tokens = diagnosis.tokens
        probe_run = grader.grade_incident(diagnosis, rem_out, diagnosis.top1.fault_class, diagnosis.top1.service)
        w.write(probe_run, diagnosis, fp, run_id, symptom)
    total_tokens = rem_out.tokens + diagnosis_tokens
    return {
        "path": rem_out.path,
        "confirm_calls": 1 if rem_out.confirm.get("tokens", 0) or rem_out.tokens else 0,
        "reasoning_calls": reasoning_calls,
        "confirm_tokens": rem_out.tokens,
        "reasoning_tokens": diagnosis_tokens,
        "total_tokens": total_tokens,
        "stage0_ms": rem_out.timings_ms.get("stage0", 0.0),
        "stage1_ms": rem_out.timings_ms.get("stage1", 0.0),
    }


first = run_incident("run_001")
second = run_incident("run_002")

print(f"{'':28}{'1st occurrence (cold)':>24}{'2nd occurrence (recurrence)':>30}")
print(f"{'path':28}{first['path']:>24}{second['path']:>30}")
print(f"{'confirm (stage-2) calls':28}{first['confirm_calls']:>24}{second['confirm_calls']:>30}")
print(f"{'reasoning (Correlator) calls':28}{first['reasoning_calls']:>24}{second['reasoning_calls']:>30}")
print(f"{'confirm tokens':28}{first['confirm_tokens']:>24}{second['confirm_tokens']:>30}")
print(f"{'reasoning tokens':28}{first['reasoning_tokens']:>24}{second['reasoning_tokens']:>30}")
print(f"{'TOTAL tokens':28}{first['total_tokens']:>24}{second['total_tokens']:>30}")

saved = first["total_tokens"] - second["total_tokens"]
pct = (saved / first["total_tokens"] * 100) if first["total_tokens"] else 0.0
print(f"\nTokens saved on recurrence: {saved} ({pct:.0f}% less than the cold run)")

assert first["path"] == "memory_miss", f"expected the first occurrence to be a cold memory_miss, got {first['path']}"
assert second["path"] == "memory_hit", f"expected the second occurrence to be a memory_hit, got {second['path']}"
assert second["reasoning_calls"] == 0, "the recurrence must never re-run the Correlator's reasoning call"
assert second["total_tokens"] < first["total_tokens"], "the recurrence must use strictly fewer tokens than the cold run"

print("\nPASSED")
