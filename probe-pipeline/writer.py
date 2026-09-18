"""Writer -- "what do we keep?" (PROBE-component-contracts.md #6).

In: a graded ProbeRun (from grader.py) + the Diagnosis it graded +
    the Fingerprint. Deliberately NOT the ground truth -- the Writer
    only ever sees `correct_at1`/`abstained`/`path`, the booleans the
    Grader derived, never `(truth_fault_class, truth_service)` itself.
    That's what makes "the truth is never written on a miss" true by
    construction rather than by discipline.
Writes: `probe-memory` (runbooks, ruled-out records), a GitHub PR.
Uses no LLM -- the steps in any runbook it writes came from the
Correlator, earlier in the pipeline.

Never: writes on an ablation run (`config != "FULL"`), writes the
truth on a miss, overwrites a human-edited runbook body, deletes a
runbook.
"""
from __future__ import annotations

import agent_builder
import es_client
from remediator import Remediator
from schemas import Diagnosis, Fingerprint, ProbeRun

HUMAN_VERIFIED_STATUSES = frozenset({"verified"})


def default_open_pr_stub(fault_class: str, service: str, diagnosis: Diagnosis) -> dict:
    """Placeholder for the GitHub PR side of the write (see
    github_runbook.py's read side, and the earlier explanation of the
    runbooks/{fault_class}__{service}.md mirror). No GitHub client/token
    is wired into this repo, so this fails soft -- it reports that
    nothing was opened rather than raising, so a missing PR integration
    doesn't take down the `probe-memory` write it's paired with.
    """
    return {"opened": False, "reason": "no GitHub client wired -- see writer.py docstring"}


class Writer:
    def __init__(self, remediator: Remediator, open_pr_fn=default_open_pr_stub) -> None:
        self._remediator = remediator
        self._open_pr_fn = open_pr_fn

    # -- probe-memory primitives ---------------------------------------

    @staticmethod
    def _runbook_id(fault_class: str, service: str) -> str:
        return f"{fault_class}__{service}"

    @staticmethod
    def _find_ruled_out_ids_for_incident(incident_id: str) -> list[str]:
        query = """
            FROM probe-memory METADATA _id
            | WHERE kind == "ruled_out" AND incident == ?incident_id
            | KEEP _id
        """
        rows = es_client.esql(query, {"incident_id": incident_id})
        return [r["_id"] for r in rows]

    def _stitch_ruled_out(self, incident_id: str, runbook_id: str) -> None:
        """Any ruled-out record tied to this same incident now has a
        confirmed answer -- point it at the runbook that turned out
        to be right.
        """
        for ruled_out_id in self._find_ruled_out_ids_for_incident(incident_id):
            es_client.update_doc("probe-memory", ruled_out_id, {"resolved_by": runbook_id})

    def _upsert_runbook(
        self, fault_class: str, service: str, diagnosis: Diagnosis, fingerprint: Fingerprint, incident_id: str, symptom: str
    ) -> str:
        runbook_id = self._runbook_id(fault_class, service)
        existing = es_client.get_doc("probe-memory", runbook_id)

        if existing is None:
            es_client.index_doc(
                "probe-memory",
                runbook_id,
                {
                    "kind": "runbook",
                    "fault_class": fault_class,
                    "service": service,
                    "status": "candidate",
                    "occurrences": 1,
                    "failed_reuses": 0,
                    "verified_by": [],
                    "root_cause": diagnosis.root_cause,
                    "steps": diagnosis.steps,
                    "symptoms": [symptom],
                    "runs": [incident_id],
                    "ruled_out_before": [],
                    "signature": {
                        "change_point": fingerprint.change_point,
                        "metric": fingerprint.metric,
                        "loudest_service": fingerprint.loudest_service,
                        "dependency": fingerprint.dependency,
                    },
                    "semantic": symptom,
                },
            )
        else:
            symptoms = list(existing.get("symptoms", []))
            new_variant = symptom not in symptoms
            if new_variant:
                symptoms.append(symptom)
            runs = list(existing.get("runs", []))
            runs.append(incident_id)
            patch = {"occurrences": existing.get("occurrences", 0) + 1, "symptoms": symptoms, "runs": runs}

            # Case D (search-cases.md): a new symptom variant must also
            # widen the `semantic` field stage 1 actually searches --
            # otherwise "next time this symptom appears, stage 1 matches
            # it more strongly" doesn't come true, it just stays true
            # for the original wording forever.
            if new_variant:
                patch["semantic"] = "\n".join(symptoms)

            # Never overwrite a human-edited runbook body: only refresh
            # root_cause/steps while the runbook is still a machine
            # draft (status "candidate"), never once it's "verified".
            if existing.get("status") not in HUMAN_VERIFIED_STATUSES:
                patch["root_cause"] = diagnosis.root_cause
                patch["steps"] = diagnosis.steps

            es_client.update_doc("probe-memory", runbook_id, patch)

        self._remediator.invalidate(runbook_id)
        self._stitch_ruled_out(incident_id, runbook_id)
        return runbook_id

    def _write_ruled_out(
        self, proposed_fault_class: str, proposed_service: str, incident_id: str, symptom: str, verified_by: str | None = None
    ) -> None:
        doc_id = f"ruledout__{incident_id}__{proposed_fault_class}__{proposed_service}"
        es_client.index_doc(
            "probe-memory",
            doc_id,
            {
                "kind": "ruled_out",
                "proposed": {"fault_class": proposed_fault_class, "service": proposed_service},
                "incident": incident_id,
                "symptom": symptom,
                "resolved_by": None,
                "verified_by": verified_by,
            },
        )

    def _demote(self, runbook_id: str) -> None:
        existing = es_client.get_doc("probe-memory", runbook_id)
        failed_reuses = (existing.get("failed_reuses", 0) if existing else 0) + 1
        es_client.update_doc("probe-memory", runbook_id, {"failed_reuses": failed_reuses, "status": "demoted"})
        self._remediator.invalidate(runbook_id)

    # -- entry point -----------------------------------------------------

    def write(self, probe_run: ProbeRun, diagnosis: Diagnosis | None, fingerprint: Fingerprint | None, incident_id: str, symptom: str) -> dict:
        if probe_run.config != "FULL":
            return {"wrote": False, "reason": "ablation run -- never touches memory"}
        if probe_run.is_null_run or probe_run.gate_kind != "incident":
            return {"wrote": False, "reason": "not a graded incident"}
        if probe_run.abstained:
            return {"wrote": False, "reason": "abstained -- writes nothing"}
        if diagnosis is None or fingerprint is None:
            raise ValueError("a graded, non-abstained incident must carry a Diagnosis and Fingerprint")

        top1 = diagnosis.top1

        if probe_run.correct_at1:
            runbook_id = self._upsert_runbook(top1.fault_class, top1.service, diagnosis, fingerprint, incident_id, symptom)
            pr = self._open_pr_fn(top1.fault_class, top1.service, diagnosis)
            return {"wrote": True, "action": "upsert_runbook", "runbook_id": runbook_id, "pr": pr}

        # Wrong. The truth is never written here -- only the proposed
        # (wrong) answer, because that's all this function ever received.
        self._write_ruled_out(top1.fault_class, top1.service, incident_id, symptom)
        result = {"wrote": True, "action": "write_ruled_out", "proposed": f"{top1.fault_class}__{top1.service}"}

        if probe_run.path == "memory_hit" and probe_run.reused_runbook_id:
            self._demote(probe_run.reused_runbook_id)
            result["demoted"] = probe_run.reused_runbook_id
        return result

    # -- verification triggers, outside the graded write path -----------

    def mark_verified_by_recovery(self, runbook_id: str) -> None:
        existing = es_client.get_doc("probe-memory", runbook_id)
        verified_by = list(existing.get("verified_by", [])) if existing else []
        if "recovery" not in verified_by:
            verified_by.append("recovery")
        es_client.update_doc("probe-memory", runbook_id, {"verified_by": verified_by})
        self._remediator.invalidate(runbook_id)

    def mark_jira_fixed(self, runbook_id: str) -> dict:
        existing = es_client.get_doc("probe-memory", runbook_id)
        verified_by = list(existing.get("verified_by", [])) if existing else []
        if "human" not in verified_by:
            verified_by.append("human")
        es_client.update_doc("probe-memory", runbook_id, {"verified_by": verified_by, "status": "verified"})
        self._remediator.invalidate(runbook_id)

        # A human closing the ticket Fixed is the one signal this runbook's
        # diagnosis was actually checked, not just self-confirmed by the
        # confirm agent -- worth a one-time write to give this service its
        # own Agent Builder candidate-query tool (agent_builder.py).
        if existing is None:
            return {"verified": True, "tool": {"provisioned": False, "reason": "no runbook doc to read fault_class/service from"}}
        tool_result = agent_builder.upsert_candidate_query_tool(
            fault_class=existing.get("fault_class"),
            service=existing.get("service"),
            root_cause=existing.get("root_cause", ""),
            steps=existing.get("steps", []),
            signature=existing.get("signature", {}),
        )
        return {"verified": True, "tool": tool_result}

    def mark_jira_rejected(self, fault_class: str, service: str, incident_id: str, symptom: str) -> None:
        self._write_ruled_out(fault_class, service, incident_id, symptom, verified_by="human")
