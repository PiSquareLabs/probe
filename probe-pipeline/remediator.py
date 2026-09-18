"""Remediator -- "have we seen this before?"
(PROBE-component-contracts.md #3, worked in full in
PROBE-runbook-search-cases.md).

Order: cache -> stage 0 (exact) -> stage 1 (hybrid) -> ruled-out (always)
-> confirm (only if >=1 candidate) -> decide.

Reads `probe-memory` only. Never reads telemetry, never reads the fault
catalog, never sees ground truth, never writes anything -- writing is
the Writer's job, triggered later by a *graded* run.

The confirm step (stage 2) is the one LLM call in this module, and the
only place this file cannot be exercised standalone: `confirm_fn` is
injected rather than hardcoded to a specific Bedrock/Agent Builder
client, because no such client exists in this repo yet (see
DETECTORS.md's own note that no Correlator/Remediator stage exists
here). `default_confirm_stub` below is a safe placeholder that always
answers "no match" -- wire in the real Agent Builder call
(PROBE-runbook-search-cases.md #2, "The confirm agent") before trusting
any hit this module reports.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import es_client
from schemas import Decision, Fingerprint, RemediatorOutput, RuledOut

CACHE_TTL_SECONDS = 10 * 60

# Provisional -- the contract is explicit that this must be set from
# Day-2 variant-run data, not guessed. Do not ship this value.
CONFIRM_THRESHOLD = 0.75


def default_confirm_stub(candidate: dict, fingerprint: Fingerprint, ruled_out: list[RuledOut]) -> dict:
    """Placeholder for the one-call Agent Builder/Bedrock confirm step.
    Always returns no-match so an unwired Remediator fails closed
    (-> memory_miss -> Correlator) instead of fabricating a hit.
    """
    return {"match": False, "confidence": 0.0}


@dataclass
class _CacheEntry:
    runbook_id: str
    stored_at: float


class Remediator:
    def __init__(self, confirm_fn=default_confirm_stub) -> None:
        self._cache: dict[str, _CacheEntry] = {}
        self._confirm_fn = confirm_fn

    # -- the fingerprint cache (contract #7a) -------------------------

    def invalidate(self, runbook_id: str) -> None:
        """Called by the Writer after any write to a runbook. The only
        cross-module call in the system besides Detector.change_point().
        """
        stale = [k for k, v in self._cache.items() if v.runbook_id == runbook_id]
        for k in stale:
            del self._cache[k]

    def _cache_get(self, fingerprint: Fingerprint) -> str | None:
        entry = self._cache.get(fingerprint.cache_key())
        if entry is None:
            return None
        if time.time() - entry.stored_at >= CACHE_TTL_SECONDS:
            del self._cache[fingerprint.cache_key()]
            return None
        return entry.runbook_id

    def _cache_set(self, fingerprint: Fingerprint, runbook_id: str) -> None:
        self._cache[fingerprint.cache_key()] = _CacheEntry(runbook_id, time.time())

    # -- the three ES|QL tools (PROBE-runbook-search-cases.md #2) -----

    @staticmethod
    def _stage0_exact(fp: Fingerprint) -> list[dict]:
        # METADATA _id + RENAME: `id` is not a real field on probe-memory
        # docs (only signature.*/status/etc. are) -- it's the document id
        # Elasticsearch tracks as metadata, which ES|QL only exposes if
        # explicitly requested via `METADATA _id` on FROM. Omitting this
        # produces a real, previously-undiscovered "Unknown column [id]"
        # failure the moment a genuine candidate exists to query for --
        # caught live when the first real incident this project produced
        # (a frontend/error_rate intermittent) finally reached this stage.
        query = """
            FROM probe-memory METADATA _id
            | WHERE kind == "runbook"
              AND status != "demoted"
              AND signature.change_point    == ?change_point
              AND signature.metric          == ?metric
              AND signature.loudest_service == ?loudest
              AND signature.dependency      == ?dependency
            | RENAME _id AS id
            | KEEP id, status, occurrences, failed_reuses, root_cause, steps, symptoms, ruled_out_before, fault_class, service
            | LIMIT 3
        """
        return es_client.esql(
            query,
            {
                "change_point": fp.change_point,
                "metric": fp.metric,
                "loudest": fp.loudest_service,
                "dependency": fp.dependency,
            },
        )

    @staticmethod
    def _stage1_hybrid(fp: Fingerprint, symptom: str) -> list[dict]:
        # Same METADATA _id / RENAME fix as _stage0_exact -- see its
        # comment. RENAME happens after FUSE since _id needs to survive
        # both FORK branches merging back together first. _score has the
        # exact same problem as _id: `SORT _score` inside a FORK branch
        # fails with "Unknown column [_score]" unless it's requested via
        # METADATA too -- found live, same crash pattern as the _id bug.
        #
        # _index is a THIRD metadata column needed here: FUSE's default
        # row-matching key is `_index`, and without requesting it via
        # METADATA the command fails outright with "FUSE requires a key
        # column, default [_index] column not found" -- found live, same
        # root cause class as the two bugs above (ES|QL only exposes
        # document metadata columns that were explicitly asked for).
        query = """
            FROM probe-memory METADATA _id, _index, _score
            | WHERE kind == "runbook" AND status != "demoted"
            | FORK
                ( WHERE MATCH(semantic, ?symptom) | SORT _score DESC | LIMIT 10 )
                ( WHERE signature.change_point == ?change_point
                    OR  signature.loudest_service == ?loudest
                    OR  signature.dependency == ?dependency
                  | SORT occurrences DESC | LIMIT 10 )
            | FUSE
            | EVAL rank = _score - (failed_reuses * 0.1)
            | SORT rank DESC
            | RENAME _id AS id
            | KEEP id, status, occurrences, root_cause, steps, symptoms, ruled_out_before, _score, fault_class, service
            | LIMIT 3
        """
        params = {
            "symptom": symptom,
            "change_point": fp.change_point,
            "loudest": fp.loudest_service,
            "dependency": fp.dependency,
        }
        try:
            return es_client.esql(query, params)
        except es_client.EsqlError:
            # FORK/FUSE unavailable on this ES version -- RRF retriever
            # fallback over the same two queries (tool 2's documented
            # fallback).
            # A null-valued term filter (e.g. fp.dependency is often
            # None) is invalid Elasticsearch JSON -- {"term": {"field":
            # null}} -- found live as an x_content_parse_exception, not
            # just "matches nothing". Only include a `should` clause
            # when its value is actually set.
            should = []
            if fp.change_point:
                should.append({"term": {"signature.change_point": fp.change_point}})
            if fp.loudest_service:
                should.append({"term": {"signature.loudest_service": fp.loudest_service}})
            if fp.dependency:
                should.append({"term": {"signature.dependency": fp.dependency}})
            body = {
                "retriever": {
                    "rrf": {
                        "retrievers": [
                            {"standard": {"query": {"semantic": {"field": "semantic", "query": symptom}}}},
                            {
                                "standard": {
                                    "query": {
                                        "bool": {
                                            "filter": [{"term": {"kind": "runbook"}}],
                                            "must_not": [{"term": {"status": "demoted"}}],
                                            "should": should,
                                        }
                                    }
                                }
                            },
                        ]
                    }
                },
                "size": 3,
            }
            result = es_client.search("probe-memory", body)
            return [
                {"id": hit["_id"], **hit["_source"], "_score": hit["_score"]} for hit in result["hits"]["hits"]
            ]

    @staticmethod
    def _ruled_out(symptom: str) -> list[RuledOut]:
        query = """
            FROM probe-memory
            | WHERE kind == "ruled_out" AND MATCH(symptom, ?symptom)
            | KEEP proposed.fault_class, proposed.service, incident, resolved_by
            | LIMIT 10
        """
        rows = es_client.esql(query, {"symptom": symptom})
        return [
            RuledOut(
                proposed_fault_class=r["proposed.fault_class"],
                proposed_service=r["proposed.service"],
                incident=r["incident"],
                resolved_by=r.get("resolved_by"),
            )
            for r in rows
        ]

    # -- confirm (stage 2) ---------------------------------------------

    def _confirm(self, candidate: dict, fingerprint: Fingerprint, ruled_out: list[RuledOut]) -> dict:
        try:
            result = self._confirm_fn(candidate, fingerprint, ruled_out)
            if not isinstance(result, dict) or "match" not in result or "confidence" not in result:
                raise ValueError("confirm response missing match/confidence")
            return {"match": bool(result["match"]), "confidence": float(result["confidence"])}
        except Exception:
            # Case J: malformed confirm response is never a hit.
            return {"match": False, "confidence": 0.0, "confirm_error": True}

    # -- entry point -----------------------------------------------------

    def remediate(self, decision: Decision, fingerprint: Fingerprint, symptom: str) -> RemediatorOutput:
        es_client.set_stage("remediator")
        if decision.kind != "incident":
            raise ValueError(
                f"Remediator runs on every incident, never on watch/transient (got kind={decision.kind!r})"
            )

        timings_ms: dict[str, float] = {"stage0": 0.0, "stage1": 0.0, "stage2": 0.0}
        candidates: list[dict] = []
        cache_hit_id = self._cache_get(fingerprint)

        if cache_hit_id is not None:
            runbook = es_client.get_doc("probe-memory", cache_hit_id)
            if runbook is not None and runbook.get("status") != "demoted":
                # get_doc() returns the raw _source, which has no "id" key
                # inside it (id is document metadata, not a real field) --
                # stage0/stage1 both RENAME _id AS id so their candidates
                # always carry it; inject it here too so matched_runbook["id"]
                # below doesn't KeyError on a cache hit. Found live: crashed
                # on the second occurrence of a repeated incident, exactly
                # the case the cache exists to serve.
                candidates = [{**runbook, "id": cache_hit_id}]
            # else: falls through to stage 0/1 below, same as a cache miss.

        if not candidates:
            t0 = time.perf_counter()
            stage0_rows = self._stage0_exact(fingerprint)
            timings_ms["stage0"] = (time.perf_counter() - t0) * 1000
            if len(stage0_rows) == 1:
                candidates = stage0_rows
            else:
                t1 = time.perf_counter()
                candidates = self._stage1_hybrid(fingerprint, symptom)
                timings_ms["stage1"] = (time.perf_counter() - t1) * 1000

        ruled_out = self._ruled_out(symptom)

        confirm_result = {"match": False, "confidence": 0.0}
        matched_runbook: dict | None = None
        if candidates:
            t2 = time.perf_counter()
            for candidate in candidates:
                confirm_result = self._confirm(candidate, fingerprint, ruled_out)
                if confirm_result["match"]:
                    matched_runbook = candidate
                    break
            timings_ms["stage2"] = (time.perf_counter() - t2) * 1000

        is_hit = matched_runbook is not None and confirm_result["confidence"] >= CONFIRM_THRESHOLD
        if is_hit:
            self._cache_set(fingerprint, matched_runbook["id"])
            return RemediatorOutput(
                path="memory_hit",
                runbook=matched_runbook,
                candidates=candidates,
                ruled_out=ruled_out,
                confirm=confirm_result,
                timings_ms=timings_ms,
            )

        # Never cache a miss (contract #7a) -- next lookup must search.
        return RemediatorOutput(
            path="memory_miss",
            runbook=None,
            candidates=candidates,
            ruled_out=ruled_out,
            confirm=confirm_result,
            timings_ms=timings_ms,
        )
