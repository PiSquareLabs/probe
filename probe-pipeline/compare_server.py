"""Local dev server for the Remediator's actual search path -- two DIFFERENT
catalog incidents, not one incident replayed twice. "Run memory_miss" runs
cartFailure (cold: store is empty, Correlator makes one REAL OpenAI
reasoning call, Writer persists the runbook). "Run memory_hit" then runs
paymentUnreachable -- a different flag, different dependency, same
change_point/metric/loudest_service -- so it deliberately CANNOT short-
circuit through the Remediator's in-process fingerprint cache or stage-0
exact match (those require an identical signature). It has to fall through
to stage-1's hybrid search (PROBE-runbook-search-cases.md #2, tool 2),
which finds cartFailure's runbook as a near-match candidate on partial
signature overlap, and then a REAL confirm call judges whether it actually
explains this new incident. That's the real job this stage does -- finding
a *similar* runbook, not replaying an identical key.

Both incidents are read straight out of catalog/cartFailure.yaml and
catalog/paymentUnreachable.yaml -- nothing here is invented.

Makes real, billed calls to the OpenAI API (gpt-4o-mini, llm_openai.py's
own MODEL) -- nothing here fakes llm_openai._chat. Needs OPENAI_API_KEY in
the environment before the server starts. es_client is still faked (no
live Elasticsearch needed -- see _fake_esql below for what stage-0/stage-1
approximate locally).

Run: OPENAI_API_KEY=sk-... uvicorn compare_server:app --port 8010
"""
import os
import sys
from pathlib import Path
from threading import Lock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")  # gitignored; see .env.example

if not os.environ.get("OPENAI_API_KEY"):
    raise SystemExit(
        "OPENAI_API_KEY not set -- this server makes real OpenAI calls and "
        "refuses to start without a key. Either export it before launch:\n"
        "  OPENAI_API_KEY=sk-... uvicorn compare_server:app --app-dir probe-pipeline --port 8010\n"
        "or put it in probe-pipeline/.env (gitignored):\n"
        "  OPENAI_API_KEY=sk-..."
    )

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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

# -- ground both scenarios in real, reviewed catalog entries -------------
_cart_flag = catalog_runbook.read_by_flag("cartFailure")
if _cart_flag is None:
    raise SystemExit("catalog/cartFailure.yaml not found or unreadable -- run this from probe-pipeline/.")
_payment_flag = catalog_runbook.read_by_flag("paymentUnreachable")
if _payment_flag is None:
    raise SystemExit("catalog/paymentUnreachable.yaml not found or unreadable -- run this from probe-pipeline/.")

# Run 1 -- cartFailure.yaml's own expected_signature/reference_runbook.
FP = Fingerprint(
    change_point="spike",
    metric="error_rate",
    loudest_service="checkout",
    dependency="valkey/redis (hardcoded to unreachable badhost:1234)",
)
INCIDENT = Decision(kind="incident", pattern="sustained", trigger=[Candidate("checkout", "error_rate", "spike", "t1", 0.001, 5.0, 2)])
SYMPTOM = f"{_cart_flag.service}'s EmptyCart RPC failing, {FP.loudest_service} seeing downstream errors"
DET_OUT = {"candidates": [Candidate("checkout", "error_rate", "spike", "t1", 0.001, 5.0, 2)], "loudest": "checkout", "earliest": "t1", "scan_at": "t1"}

# Run 2 -- paymentUnreachable.yaml: a DIFFERENT flag/dependency, same
# change_point/metric/loudest_service as cartFailure. Stage-0's exact
# 4-field match fails on purpose (dependency differs) -- this can only be
# found through stage-1's hybrid search matching on the overlap.
FP2 = Fingerprint(
    change_point="spike",
    metric="error_rate",
    loudest_service="checkout",
    dependency="payment (client redirected to unreachable badAddress:50051)",
)
INCIDENT2 = Decision(kind="incident", pattern="sustained", trigger=[Candidate("checkout", "error_rate", "spike", "t2", 0.001, 4.6, 2)])
SYMPTOM2 = f"{_payment_flag.service}'s chargeCard RPC failing, {FP2.loudest_service} seeing downstream errors"
DET_OUT2 = {"candidates": [Candidate("checkout", "error_rate", "spike", "t2", 0.001, 4.6, 2)], "loudest": "checkout", "earliest": "t2", "scan_at": "t2"}

_lock = Lock()
_state = {"store": None, "remediator": None, "correlator": None, "writer": None, "runs": []}


def _fake_esql(query: str, params: dict | None = None) -> list[dict]:
    """Local stand-in for probe-memory's two search tools (no live
    Elasticsearch here). Stage 0 is exact -- unchanged from before. Stage 1
    approximates the real FORK/FUSE query's two branches: signature-field
    overlap (change_point/loudest_service/dependency, same OR the real
    query uses) plus a naive keyword-overlap stand-in for MATCH(semantic,
    ...), since there's no real semantic index to score against locally.
    """
    store = _state["store"]
    if 'kind == "ruled_out"' in query:
        return []
    if "FUSE" in query:
        symptom_words = {w.lower() for w in (params.get("symptom") or "").split() if len(w) > 3}
        scored = []
        for doc_id, doc in store.items():
            if doc.get("kind") != "runbook" or doc.get("status") == "demoted":
                continue
            sig = doc.get("signature", {})
            signature_overlap = sum(
                [
                    sig.get("change_point") == params.get("change_point"),
                    sig.get("loudest_service") == params.get("loudest"),
                    sig.get("dependency") == params.get("dependency"),
                ]
            )
            if signature_overlap == 0:
                continue
            doc_text = " ".join(doc.get("symptoms", []) or []) + " " + str(doc.get("root_cause", ""))
            doc_words = {w.lower() for w in doc_text.split() if len(w) > 3}
            keyword_overlap = len(symptom_words & doc_words)
            scored.append({**doc, "id": doc_id, "_score": signature_overlap * 2 + keyword_overlap * 0.5})
        scored.sort(key=lambda d: d["_score"], reverse=True)
        return scored[:3]
    if "signature.change_point" in query and "FUSE" not in query:
        for doc_id, doc in store.items():
            if doc.get("kind") != "runbook" or doc.get("status") == "demoted":
                continue
            sig = doc.get("signature", {})
            if (
                sig.get("change_point") == params["change_point"]
                and sig.get("metric") == params["metric"]
                and sig.get("loudest_service") == params["loudest"]
                and sig.get("dependency") == params["dependency"]
            ):
                return [{**doc, "id": doc_id}]
        return []
    return []


def reset_state() -> None:
    store: dict[str, dict] = {}
    es_client.get_doc = lambda idx, doc_id: store.get(doc_id)
    es_client.index_doc = lambda idx, doc_id, body: store.update({doc_id: dict(body)})
    es_client.update_doc = lambda idx, doc_id, patch: store[doc_id].update(patch)
    es_client.esql = _fake_esql

    remediator = Remediator(confirm_fn=llm_openai.confirm)
    correlator = Correlator(service_graph_path=Path("/nonexistent.json"), change_point_fn=lambda s, sig: None, reasoning_fn=llm_openai.reason)
    _state.update(
        store=store,
        remediator=remediator,
        correlator=correlator,
        writer=writer.Writer(remediator),
        runs=[],
    )


reset_state()

app = FastAPI(title="Probe -- memory hit/miss comparison")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _run_incident(run_id: str, incident: Decision, fp: Fingerprint, symptom: str, det_out: dict) -> dict:
    remediator = _state["remediator"]
    correlator = _state["correlator"]
    w = _state["writer"]

    rem_out = remediator.remediate(incident, fp, symptom)
    reasoning_calls = 0
    diagnosis_tokens = 0
    llm_answer = None
    llm_error = bool(rem_out.confirm.get("confirm_error"))
    found_via = (
        "stage1 (similar -- hybrid search)"
        if rem_out.timings_ms.get("stage1")
        else "stage0 (exact match)"
        if rem_out.timings_ms.get("stage0")
        else None
    )
    written_runbook = None
    if rem_out.path == "memory_miss":
        diagnosis, _evidence = correlator.correlate(incident, det_out, rem_out, symptom)
        reasoning_calls = 1
        diagnosis_tokens = diagnosis.tokens
        probe_run = grader.grade_incident(diagnosis, rem_out, diagnosis.top1.fault_class, diagnosis.top1.service)
        write_result = w.write(probe_run, diagnosis, fp, run_id, symptom)
        runbook_id = write_result.get("runbook_id")
        if runbook_id:
            written_runbook = {"id": runbook_id, "steps": diagnosis.steps}
        llm_answer = f"{diagnosis.top1.fault_class}/{diagnosis.top1.service} -- {diagnosis.root_cause}"
    else:
        matched_cause = (rem_out.runbook or {}).get("root_cause", "")
        llm_answer = f"reused {rem_out.runbook.get('id')} (confidence {rem_out.confirm.get('confidence')}) -- {matched_cause}"
    total_tokens = rem_out.tokens + diagnosis_tokens
    return {
        "run_id": run_id,
        "path": rem_out.path,
        "confirm_calls": 1 if rem_out.confirm.get("tokens", 0) or rem_out.tokens else 0,
        "reasoning_calls": reasoning_calls,
        "confirm_tokens": rem_out.tokens,
        "reasoning_tokens": diagnosis_tokens,
        "total_tokens": total_tokens,
        "stage0_ms": rem_out.timings_ms.get("stage0", 0.0),
        "stage1_ms": rem_out.timings_ms.get("stage1", 0.0),
        "found_via": found_via,
        "candidates_considered": len(rem_out.candidates),
        "written_runbook": written_runbook,
        "llm_answer": llm_answer,
        "llm_error": llm_error,
    }


@app.post("/api/reset")
def api_reset():
    with _lock:
        reset_state()
    return {"ok": True}


@app.post("/api/run/{which}")
def api_run(which: str):
    if which not in ("miss", "hit"):
        raise HTTPException(404, f"no such run: {which}")

    with _lock:
        run_id = "run_001" if which == "miss" else "run_002"
        already = any(r["run_id"] == run_id for r in _state["runs"])
        if already:
            raise HTTPException(409, f"{which} already ran this session -- reset to run it again")
        if which == "hit" and not any(r["run_id"] == "run_001" for r in _state["runs"]):
            raise HTTPException(409, "run memory_miss (cartFailure) first -- paymentUnreachable's search needs a runbook already in probe-memory")

        if which == "miss":
            result = _run_incident(run_id, INCIDENT, FP, SYMPTOM, DET_OUT)
        else:
            result = _run_incident(run_id, INCIDENT2, FP2, SYMPTOM2, DET_OUT2)

        _state["runs"].append(result)
        return result


@app.get("/api/info")
def api_info():
    return {
        "model": llm_openai.MODEL,
        "run1_flag": _cart_flag.flag,
        "run1_symptom": SYMPTOM,
        "run2_flag": _payment_flag.flag,
        "run2_symptom": SYMPTOM2,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "compare_ui.html").read_text(encoding="utf-8")
