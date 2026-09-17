"""OpenAI-backed implementations of the two LLM calls the contract
allows: the Remediator's stage-2 confirm
(PROBE-runbook-search-cases.md #2, "The confirm agent") and the
Correlator's one reasoning call (PROBE-component-contracts.md #4 step 6).

Model: gpt-4o-mini. Swap providers later by writing a function with the
same signature and passing it to Remediator(confirm_fn=...) /
Correlator(reasoning_fn=...) -- nothing else in probe-pipeline/ imports
this module, matching the existing fail-closed-stub pattern in
remediator.py/correlator.py. Neither class's *default* was changed to
point here: importing/instantiating them still requires no network
access or API key, exactly as before. Pass these functions in
explicitly:

    from remediator import Remediator
    from correlator import Correlator
    import llm_openai
    remediator = Remediator(confirm_fn=llm_openai.confirm)
    correlator = Correlator(reasoning_fn=llm_openai.reason)

Requires OPENAI_API_KEY in the environment. Uses raw urllib (matching
this repo's own stdlib-only HTTP client convention -- es_client.py,
github_runbook.py) instead of adding the `openai` package as a
dependency.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from schemas import FAULT_CLASSES, Fingerprint, RuledOut

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"


class LlmError(RuntimeError):
    pass


def _chat(system: str, user: str, max_tokens: int) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LlmError("OPENAI_API_KEY not set")
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise LlmError(f"OpenAI request failed ({e.code}): {e.read().decode()}") from e
    return result["choices"][0]["message"]["content"]


# -- Remediator stage 2 (search-cases.md #2, "The confirm agent") -------

_CONFIRM_SYSTEM = (
    "You are the confirm step in an incident-response memory lookup. "
    "You are given one candidate runbook and one change point. Decide "
    "only whether the candidate explains the change point -- do not "
    'reason about anything else. Respond with JSON only: {"match": '
    'true|false, "confidence": 0..1}. No other text.'
)


def confirm(candidate: dict, fingerprint: Fingerprint, ruled_out: list[RuledOut]) -> dict:
    """One candidate per call -- remediator.py's own loop is what tries
    top-1, then top-2 on rejection (search-cases.md Case H); this
    function only ever judges the single candidate it's given.
    """
    ruled_out_text = ", ".join(f"{r.proposed_fault_class}/{r.proposed_service}" for r in ruled_out) or "none"
    user = (
        f"Change point: {fingerprint.change_point} on {fingerprint.metric}, "
        f"loudest {fingerprint.loudest_service}, dependency {fingerprint.dependency}\n"
        f"Candidate: {candidate.get('id')} — {candidate.get('root_cause')} — "
        f"seen {candidate.get('occurrences')}×, status {candidate.get('status')}\n"
        f"Ruled out for this symptom: {ruled_out_text}\n"
        "Does this runbook explain this change point?"
    )
    try:
        raw = _chat(_CONFIRM_SYSTEM, user, max_tokens=50)
        parsed = json.loads(raw)
        return {"match": bool(parsed["match"]), "confidence": float(parsed["confidence"])}
    except Exception:
        # Case J: malformed JSON, an API error, or a missing key is
        # never a hit -- remediator.py's own _confirm() wrapper already
        # enforces this too, but failing closed here as well means this
        # function is safe even if called directly.
        return {"match": False, "confidence": 0.0, "confirm_error": True}


# -- Correlator reasoning (contract #4 step 6) ---------------------------

_REASONING_SYSTEM = (
    "You are the root-cause reasoning step of an incident-response pipeline. "
    "You are given an evidence block gathered by other tools: graph ranking, "
    "the deepest anomalous span, correlated span attributes, recent "
    "configuration changes, one extra change-point hop, near-matches from "
    "memory, and answers already ruled out for this symptom. Propose up to "
    "3 ranked candidates, most likely first. "
    f"fault_class must be exactly one of: {sorted(FAULT_CLASSES)}. "
    "Never propose a (fault_class, service) pair that appears in the "
    "evidence's ruled_out list. If nothing in the evidence supports a "
    'confident answer, return a single candidate with fault_class "unknown". '
    "Respond with JSON only, no other text: "
    '{"candidates": [{"fault_class": str, "service": str, "confidence": 0..1}, ...], '
    '"root_cause": str, "steps": [str, ...]}'
)


def reason(evidence: dict, symptom: str) -> dict:
    """The Correlator's one reasoning call. correlator.py itself
    enforces that this only ever runs on a memory_miss and re-validates
    fault_class against the taxonomy after the fact -- this function
    just answers the evidence it's given, and fails closed to
    "unknown" on any error rather than fabricating a cause.
    """
    user = f"Symptom: {symptom}\nEvidence:\n{json.dumps(evidence, default=str, indent=2)}"
    try:
        raw = _chat(_REASONING_SYSTEM, user, max_tokens=500)
        parsed = json.loads(raw)
        candidates = parsed.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("no candidates returned")
        return {
            "candidates": candidates[:3],
            "root_cause": str(parsed.get("root_cause", "")),
            "steps": list(parsed.get("steps", [])),
        }
    except Exception:
        fallback_service = evidence["graph_rank"][0]["service"] if evidence.get("graph_rank") else "unknown"
        return {
            "candidates": [{"fault_class": "unknown", "service": fallback_service, "confidence": 0.0}],
            "root_cause": "reasoning call failed or returned malformed JSON",
            "steps": [],
        }
