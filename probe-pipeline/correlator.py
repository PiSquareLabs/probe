"""Correlator -- "why?" (PROBE-component-contracts.md #4).

In: incident Decision + Detector output + Remediator output
    (candidates -> near_matches, ruled_out).
Reads: traces-*, service_graph.json, probe-changes.
Calls: an injected change_point(service, signal) for one extra hop --
    Detector.change_point() doesn't exist yet in this repo (there is no
    orchestrator wiring any of the four probe-*/ml-flag-detection
    detectors to anything downstream), so it's a constructor dependency
    here rather than an import of "another module's internals".
Uses one LLM reasoning call (injected, same fail-closed-stub pattern as
    remediator.py's confirm_fn, for the same reason: no Bedrock/Agent
    Builder client exists in this repo to wire in for real).
Owns the service graph -- no other module reads service_graph.json.

Runs on memory_miss only. Never runs on a memory hit, never sees ground
truth or the fault catalog, never grades itself, never writes to
`probe-memory` (that's the Writer's job, after the Grader has scored
this Diagnosis).
"""
from __future__ import annotations

import json
from pathlib import Path

import detector_bridge
import es_client
from schemas import FAULT_CLASSES, Candidate, Decision, Diagnosis, DiagnosisCandidate, RemediatorOutput

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERVICE_GRAPH = REPO_ROOT / "probe-detector" / "service_graph.json"

# Same data stream the real Detector's Tier 2 scans
# (../probe-two-tier-detector-v3/change_point.py's TRACES_DATA_STREAM) --
# was a guessed "traces-generic.otel-probe" before that Detector existed.
TRACES_DATA_STREAM = "traces-generic.otel-default"

GRAPH_RANK_EARLINESS_WEIGHT = 0.6
GRAPH_RANK_DEPENDED_ON_BY_WEIGHT = 0.4
RECENT_CHANGES_WINDOW_MINUTES = 5


def default_reasoning_stub(evidence: dict, symptom: str) -> dict:
    """Placeholder for the one Bedrock/Agent Builder reasoning call.
    Fails closed to `unknown` rather than fabricating a root cause when
    unwired -- wire in the real call (contract #4 step 6) before
    trusting any Diagnosis this module returns.
    """
    return {
        "candidates": [{"fault_class": "unknown", "service": evidence["graph_rank"][0]["service"] if evidence["graph_rank"] else "unknown", "confidence": 0.0}],
        "root_cause": "unscored -- reasoning_fn not wired",
        "steps": [],
    }


def default_change_point_stub(service: str, signal: str) -> dict | None:
    """Fallback for when detector_bridge's real change_point() can't
    reach a cluster (e.g. unit tests with no Elasticsearch). Always
    reports "no break" (None), matching the contract's own `extra_hops`
    example shape (`{"service": ..., "type": null}`).
    """
    return None


class Correlator:
    def __init__(
        self,
        service_graph_path: Path = DEFAULT_SERVICE_GRAPH,
        change_point_fn=detector_bridge.change_point_for_correlator,
        reasoning_fn=default_reasoning_stub,
    ) -> None:
        self._graph = self._load_graph(service_graph_path)
        self._change_point_fn = change_point_fn
        self._reasoning_fn = reasoning_fn

    @staticmethod
    def _load_graph(path: Path) -> dict:
        if not path.exists():
            return {"edges": []}
        return json.loads(path.read_text())

    def _callees(self, service: str) -> list[str]:
        return sorted(
            {
                edge["callee"]
                for edge in self._graph.get("edges", [])
                if edge.get("caller") == service
            }
        )

    # -- evidence pieces (contract #4) ---------------------------------

    def _graph_rank(self, candidates: list[Candidate]) -> list[dict]:
        """0.6 x earliness + 0.4 x depended-on-by, over the Detector's
        own candidate services -- no ES|QL LOOKUP JOIN yet (that's
        section 10.1's future move), just the JSON graph in Python.
        """
        services = {c.service for c in candidates}
        if not services:
            return []
        timestamps = [c.timestamp for c in candidates if c.timestamp]
        earliest = min(timestamps) if timestamps else None

        scored = []
        for service in services:
            service_candidates = [c for c in candidates if c.service == service and c.timestamp]
            earliness = 0.0
            if earliest and service_candidates:
                # Earlier change point -> higher earliness score. Cheap
                # proxy: 1.0 if this service tied for earliest, else 0.0 --
                # a real timestamp-delta score belongs in section 10.1's
                # ES|QL move, not reinvented here.
                earliness = 1.0 if min(c.timestamp for c in service_candidates) == earliest else 0.0
            depended_on_by = sum(
                1
                for edge in self._graph.get("edges", [])
                if edge.get("callee") == service and edge.get("caller") in services
            )
            score = GRAPH_RANK_EARLINESS_WEIGHT * earliness + GRAPH_RANK_DEPENDED_ON_BY_WEIGHT * min(
                depended_on_by / max(len(services) - 1, 1), 1.0
            )
            scored.append({"service": service, "score": round(score, 4)})
        return sorted(scored, key=lambda r: r["score"], reverse=True)

    @staticmethod
    def _deepest_span_and_correlated(loudest_service: str, cp_time: str | None) -> tuple[dict | None, list[str]]:
        """One ES|QL statement doing double duty: its top row is the
        deepest anomalous CLIENT span (-> `dependency`), and the full
        top-3 is the `correlated` attribute list (contract #10.2's
        before/after jump query, reused here rather than re-derived).
        """
        if cp_time is None:
            return None, []
        # Same field/value fix as change_point.py -- see es_client.py's
        # SPAN_KIND_FIELD comment. This query's own hardcoded
        # span.kind == "CLIENT" had the identical bug, undiscovered
        # until this function got reused for pre-search enrichment.
        query = f"""
            FROM {TRACES_DATA_STREAM}
            | WHERE service.name == ?loudest AND {es_client.SPAN_KIND_FIELD} == "{es_client.SPAN_KIND_CLIENT_VALUE}"
              AND @timestamp > ?cp_time - 5 minutes
            | EVAL phase = CASE(@timestamp >= ?cp_time, "after", "before")
            | STATS p95 = PERCENTILE(duration, 95) BY phase, db.system, peer.service, span.name
            | STATS before = MAX(CASE(phase == "before", p95, null)),
                    after  = MAX(CASE(phase == "after",  p95, null))
                    BY db.system, peer.service, span.name
            | EVAL jump = after / before
            | WHERE jump > 2
            | SORT jump DESC | LIMIT 3
        """
        try:
            rows = es_client.esql(query, {"loudest": loudest_service, "cp_time": cp_time})
        except es_client.EsqlError:
            return None, []
        if not rows:
            return None, []
        top = rows[0]
        deepest_span = {
            "name": top.get("span.name"),
            "service": loudest_service,
            "kind": es_client.SPAN_KIND_CLIENT_VALUE,
            "dependency": top.get("peer.service") or top.get("db.system"),
        }
        correlated = [f"db.system={r.get('db.system')}", f"span.name={r.get('span.name')}"] if rows else []
        return deepest_span, correlated

    @staticmethod
    def _recent_changes(before_time: str | None) -> list[dict]:
        if before_time is None:
            return []
        query = """
            FROM probe-changes
            | WHERE @timestamp < ?before AND @timestamp > ?before - 5 minutes
            | LIMIT 20
        """
        try:
            return es_client.esql(query, {"before": before_time})
        except es_client.EsqlError:
            return []

    def _extra_hops(self, loudest_service: str) -> list[dict]:
        """One extra hop: change_point() on the loudest service's
        callees. Not a walk of the whole graph -- contract #4 step 5
        says one hop.

        `change_point_fn` returns the real Detector's own shape --
        `{"type", "timestamp", "pvalue", "breaks"}` on a confirmed
        break, `{"type": None, ..., "reason": "insufficient_data"}`
        on a too-sparse series, or `None` on a genuine no-break --
        never a schemas.Candidate, since the caller (this method)
        already knows the service; only `type` is used here, matching
        the contract's own `extra_hops` example.
        """
        hops = []
        for callee in self._callees(loudest_service):
            cp = self._change_point_fn(callee, "p95_latency")
            hops.append({"service": callee, "type": cp["type"] if cp else None})
        return hops

    # -- entry point -----------------------------------------------------

    def correlate(
        self,
        decision: Decision,
        detector_output: dict,
        remediator_output: RemediatorOutput,
        symptom: str,
    ) -> tuple[Diagnosis, dict]:
        es_client.set_stage("correlator")
        if remediator_output.path != "memory_miss":
            raise ValueError(
                f"Correlator runs on memory_miss only (got path={remediator_output.path!r})"
            )

        candidates: list[Candidate] = detector_output.get("candidates", [])
        timestamps = [c.timestamp for c in candidates if c.timestamp]
        earliest_time = min(timestamps) if timestamps else None
        loudest = detector_output.get("loudest") or (decision.trigger[0].service if decision.trigger else None)

        graph_rank = self._graph_rank(candidates)
        deepest_span, correlated = self._deepest_span_and_correlated(loudest, earliest_time)
        recent_changes = self._recent_changes(earliest_time)
        extra_hops = self._extra_hops(loudest) if loudest else []

        evidence = {
            "graph_rank": graph_rank,
            "deepest_span": deepest_span,
            "correlated": correlated,
            "recent_changes": recent_changes,
            "extra_hops": extra_hops,
            "supporting": [
                {"service": c.service, "signal": c.signal} for c in decision.supporting_evidence
            ],
            "near_matches": remediator_output.candidates,
            "ruled_out": [
                {"proposed": f"{r.proposed_fault_class}__{r.proposed_service}", "incident": r.incident}
                for r in remediator_output.ruled_out
            ],
        }

        reasoning = self._reasoning_fn(evidence, symptom)

        validated_candidates = []
        for c in reasoning.get("candidates", []):
            fault_class = c["fault_class"] if c["fault_class"] in FAULT_CLASSES else "unknown"
            validated_candidates.append(
                DiagnosisCandidate(fault_class=fault_class, service=c["service"], confidence=c["confidence"])
            )
        if not validated_candidates:
            validated_candidates = [DiagnosisCandidate(fault_class="unknown", service=loudest or "unknown", confidence=0.0)]

        diagnosis = Diagnosis(
            candidates=validated_candidates[:3],
            root_cause=reasoning.get("root_cause", ""),
            symptom=symptom,
            steps=reasoning.get("steps", []),
            source="correlator",
            tokens=int(reasoning.get("tokens", 0)),
        )
        return diagnosis, evidence
