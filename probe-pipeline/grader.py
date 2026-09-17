"""Grader -- "was it right?" (PROBE-component-contracts.md #5).

In: a Diagnosis + the harness's recorded truth (fault_class, service) +
    timings + baseline answers, OR (for a null/must-not-open window)
    just the Gate's decision kind.
Reads nothing. Uses no LLM -- ground truth comes from the fault
injector only, never from a model, and this is the only module allowed
to see it (contract #7's leakage boundary table).

Never: consults a model, softens a mismatch, lets an out-of-taxonomy
answer count as correct.
"""
from __future__ import annotations

from schemas import FAULT_CLASSES, Diagnosis, ProbeRun, RemediatorOutput


def _class_service_match(candidate, truth_fault_class: str, truth_service: str) -> bool:
    """Both fault_class AND service must match. A candidate whose
    fault_class isn't even in the closed taxonomy can never count as
    correct, regardless of what string it happens to equal --
    protects against a corrupted/legacy doc smuggling a match through.
    """
    return (
        candidate.fault_class in FAULT_CLASSES
        and candidate.fault_class != "unknown"
        and candidate.fault_class == truth_fault_class
        and candidate.service == truth_service
    )


def grade_incident(
    diagnosis: Diagnosis,
    remediator_output: RemediatorOutput,
    truth_fault_class: str,
    truth_service: str,
    *,
    config: str = "FULL",
    injected_at_ms: float | None = None,
    detected_at_ms: float | None = None,
    answered_at_ms: float | None = None,
    tokens: int | None = None,
    cost_usd: float | None = None,
    baseline_answers: dict | None = None,
) -> ProbeRun:
    """Grades one incident's Diagnosis against the injected truth.
    `remediator_output` is only consulted for `path` and, on a memory
    hit, which runbook was reused -- never for anything the Correlator
    or Remediator themselves concluded about the cause.
    """
    top1 = diagnosis.top1
    correct_at1 = _class_service_match(top1, truth_fault_class, truth_service)
    correct_at3 = correct_at1 or any(
        _class_service_match(c, truth_fault_class, truth_service) for c in diagnosis.candidates[1:3]
    )
    abstained = top1.fault_class == "unknown"

    detection_error_ms = None
    if injected_at_ms is not None and detected_at_ms is not None:
        detection_error_ms = detected_at_ms - injected_at_ms
    time_to_answer_ms = None
    if detected_at_ms is not None and answered_at_ms is not None:
        time_to_answer_ms = answered_at_ms - detected_at_ms

    return ProbeRun(
        config=config,
        gate_kind="incident",
        is_null_run=False,
        correct_at1=correct_at1,
        correct_at3=correct_at3,
        abstained=abstained,
        path=remediator_output.path,
        reused_runbook_id=(
            remediator_output.runbook["id"]
            if remediator_output.path == "memory_hit" and remediator_output.runbook
            else None
        ),
        detection_time_ms=detected_at_ms,
        detection_error_ms=detection_error_ms,
        time_to_answer_ms=time_to_answer_ms,
        tokens=tokens,
        cost_usd=cost_usd,
        baseline_answers=baseline_answers or {},
    )


def grade_null_window(gate_kind: str, *, config: str = "FULL") -> ProbeRun:
    """A window where no fault was injected. Correct iff the Gate
    stayed quiet (watch or transient); an `incident` here is a false
    positive -- there was nothing to find.
    """
    if gate_kind not in ("watch", "transient", "incident"):
        raise ValueError(f"unexpected gate_kind={gate_kind!r}")
    return ProbeRun(
        config=config,
        gate_kind=gate_kind,
        is_null_run=True,
        correct_at1=gate_kind in ("watch", "transient"),
    )


def grade_must_not_open(gate_kind: str, *, config: str = "FULL") -> ProbeRun:
    """A window with a resource-only fault flag active (cpu/memory/log
    errors, never a symptom signal on their own). Correct iff the Gate
    returned `watch` specifically -- contract #5 doesn't extend the
    "or transient" leniency it gives null runs to this case, since a
    resource-only anomaly should never even count a transient bump.
    """
    return ProbeRun(
        config=config,
        gate_kind=gate_kind,
        is_null_run=False,
        correct_at1=gate_kind == "watch",
    )
