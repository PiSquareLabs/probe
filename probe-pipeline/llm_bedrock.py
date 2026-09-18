"""Amazon Bedrock-backed implementation of the two LLM calls the
contract allows: the Remediator's stage-2 confirm
(PROBE-runbook-search-cases.md #2, "The confirm agent") and the
Correlator's one reasoning call (PROBE-component-contracts.md #4 step 6).
Same two function signatures as llm_openai.py -- "Swap providers later by
writing a function with the same signature" is llm_openai.py's own
stated design, and probe-detector/README.md's own 3-agent pitch says the
Correlator is "Powered by Claude on Amazon Bedrock." The Converse API
this module calls is model-agnostic, though -- MODEL_ID defaults to
whatever's actually ACTIVE on the account this was verified against
(`GET /foundation-models` there lists openai.gpt-5.6-terra as active and
every anthropic.claude-3-* id as LEGACY/end-of-life), so don't assume
the default is Claude without checking BEDROCK_MODEL_ID.

Auth: a Bedrock API key (the "bedrock-api-key-..." string from the
console/CLI, a base64 bearer token good for up to 12h), read from
AWS_BEARER_TOKEN_BEDROCK -- never hardcoded here, never written to a
tracked file. Uses the Bedrock Runtime Converse API directly over raw
urllib (matching this repo's stdlib-only HTTP client convention --
es_client.py, llm_openai.py, github_runbook.py), not boto3, so no new
dependency is needed for a single endpoint.

Nothing in remediator.py/correlator.py imports this module by default,
same fail-closed-stub pattern as llm_openai.py -- pass these functions
in explicitly:

    from remediator import Remediator
    from correlator import Correlator
    import llm_bedrock
    remediator = Remediator(confirm_fn=llm_bedrock.confirm)
    correlator = Correlator(reasoning_fn=llm_bedrock.reason)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from schemas import FAULT_CLASSES, Fingerprint, RuledOut

# openai.gpt-5.6-terra shows ACTIVE in this region's model catalog but
# returns 403 ("not available for this account") on both bearer tokens
# tested against it -- that's a per-account Bedrock model-access grant,
# not a token problem, and it isn't something this module can work
# around. openai.gpt-oss-120b-1:0 is the model actually confirmed
# invokable (200) on this account, so that's the default until access
# to a non-OSS model is granted in the Bedrock console.
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "openai.gpt-oss-120b-1:0")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")


class LlmError(RuntimeError):
    pass


def _chat(system: str, user: str, max_tokens: int) -> tuple[str, int]:
    """Returns (content, total_tokens) -- total_tokens from Bedrock's own
    usage.totalTokens, the same real-usage convention llm_openai.py's
    _chat() uses (see that module's note on why this used to be thrown
    away).
    """
    api_key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if not api_key:
        raise LlmError("AWS_BEARER_TOKEN_BEDROCK not set")
    body = {
        "messages": [{"role": "user", "content": [{"text": user}]}],
        "system": [{"text": system}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
    }
    url = f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com/model/{MODEL_ID}/converse"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise LlmError(f"Bedrock request failed ({e.code}): {e.read().decode()}") from e

    total_tokens = result.get("usage", {}).get("totalTokens", 0)
    # gpt-oss (and other reasoning-capable Bedrock models) return a
    # content array that can hold a `reasoningContent` block ahead of --
    # or, if maxTokens ran out mid-thought (stopReason "max_tokens"),
    # *instead of* -- the actual `text` answer block. content[0]["text"]
    # KeyErrors on the reasoning block; find the real text block instead,
    # and say plainly when there isn't one rather than crash on a raw
    # KeyError that gives no hint why.
    blocks = result["output"]["message"]["content"]
    text = next((b["text"] for b in blocks if "text" in b), None)
    if text is None:
        raise LlmError(
            f"model spent its whole max_tokens={max_tokens} budget on reasoning and never "
            f"reached a text answer (stopReason={result.get('stopReason')!r}) -- raise max_tokens"
        )
    content = text
    return content, total_tokens


def _extract_json(raw: str) -> dict:
    """Claude has no OpenAI-style response_format=json_object toggle on
    Bedrock's Converse API -- the system prompt asks for "JSON only, no
    other text," which it follows almost always, but strips any stray
    fencing/prose defensively rather than trusting that unconditionally.
    """
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object found in response: {raw!r}")
    return json.loads(raw[start : end + 1])


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
    tokens = 0
    try:
        raw, tokens = _chat(_CONFIRM_SYSTEM, user, max_tokens=50)
        parsed = _extract_json(raw)
        return {"match": bool(parsed["match"]), "confidence": float(parsed["confidence"]), "tokens": tokens}
    except Exception:
        # Case J: malformed JSON, an API error, or a missing key is
        # never a hit -- remediator.py's own _confirm() wrapper already
        # enforces this too, but failing closed here as well means this
        # function is safe even if called directly.
        return {"match": False, "confidence": 0.0, "confirm_error": True, "tokens": tokens}


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
    tokens = 0
    try:
        raw, tokens = _chat(_REASONING_SYSTEM, user, max_tokens=500)
        parsed = _extract_json(raw)
        candidates = parsed.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("no candidates returned")
        return {
            "candidates": candidates[:3],
            "root_cause": str(parsed.get("root_cause", "")),
            "steps": list(parsed.get("steps", [])),
            "tokens": tokens,
        }
    except Exception:
        fallback_service = evidence["graph_rank"][0]["service"] if evidence.get("graph_rank") else "unknown"
        return {
            "candidates": [{"fault_class": "unknown", "service": fallback_service, "confidence": 0.0}],
            "root_cause": "reasoning call failed or returned malformed JSON",
            "steps": [],
            "tokens": tokens,
        }
