"""Shared objects passed between PROBE's pipeline modules, per
PROBE-component-contracts.md section 0. Nothing here talks to
Elasticsearch or an LLM -- these are just the data shapes the harness
hands from one module to the next.

None of gate.py / remediator.py / correlator.py import each other's
internals; they only share these shapes and (for remediator/correlator)
es_client.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Closed fault_class taxonomy (catalog/*.yaml uses the same six values).
# The Correlator must map anything outside this set to "unknown"
# (PROBE-component-contracts.md #4 step 7).
FAULT_CLASSES = frozenset(
    {
        "upstream_dependency_latency",
        "upstream_dependency_error",
        "resource_exhaustion",
        "error_injection",
        "queue_backpressure",
        "traffic_surge",
        "unknown",
    }
)

ChangePointType = Literal["step_change", "spike", "dip", "distribution_change", "trend_change"]


@dataclass
class Candidate:
    """One Detector finding. `type`/`pvalue` are None and `tier` is 1
    when CHANGE_POINT didn't confirm the tier-1 z-score shortlist entry.

    `reason`/`breaks` mirror the real Detector's output
    (probe-two-tier-detector-v3/change_point.py) -- `reason` distinguishes
    "insufficient_data" from a genuine no-break when tier stays 1, and
    `breaks` carries every significant break CHANGE_POINT found (not
    just the earliest one this dataclass's own `type`/`timestamp`/
    `pvalue` report) for a caller that cares about a later recovery dip.
    Both optional and trailing so existing positional construction
    elsewhere in probe-pipeline doesn't break.
    """

    service: str
    signal: str
    type: ChangePointType | None
    timestamp: str | None
    pvalue: float | None
    z: float
    tier: Literal[1, 2]
    reason: str | None = None
    breaks: list[dict] | None = None


@dataclass
class Fingerprint:
    """Assembled by the harness from Detector output plus the
    Correlator's evidence queries. `dependency`/`hops_to_cause` are None
    until the deepest-span query has run -- the Remediator may see a
    partial Fingerprint (contract #3).
    """

    change_point: ChangePointType | None
    metric: str
    loudest_service: str
    dependency: str | None = None
    hops_to_cause: int | None = None

    def cache_key(self) -> str:
        """hash(fp.change_point, fp.metric, fp.loudest_service, fp.dependency)
        per PROBE-component-contracts.md #7a.
        """
        return "|".join(
            str(part) for part in (self.change_point, self.metric, self.loudest_service, self.dependency)
        )


@dataclass
class DiagnosisCandidate:
    fault_class: str
    service: str
    confidence: float


@dataclass
class Diagnosis:
    candidates: list[DiagnosisCandidate]
    root_cause: str
    symptom: str
    steps: list[str]
    source: Literal["memory_hit", "correlator"]

    @property
    def top1(self) -> DiagnosisCandidate:
        return self.candidates[0]


@dataclass
class Decision:
    """Gate output (contract #2)."""

    kind: Literal["incident", "transient", "watch"]
    pattern: Literal["sustained", "intermittent"] | None
    trigger: list[Candidate]
    supporting_evidence: list[Candidate] = field(default_factory=list)
    evidence_timestamps: list[str] = field(default_factory=list)


@dataclass
class RuledOut:
    proposed_fault_class: str
    proposed_service: str
    incident: str
    resolved_by: str | None = None


@dataclass
class ProbeRun:
    """The Grader's one output document (contract #5) -- what the
    Writer receives instead of ground truth. `truth` itself is never
    part of this shape; only the booleans the Grader derived from it
    cross the leakage boundary into the Writer.
    """

    config: str  # "FULL" or an ablation name
    gate_kind: Literal["incident", "transient", "watch"]
    is_null_run: bool  # True when no fault was injected this window

    # Populated only when gate_kind == "incident" and a Diagnosis exists:
    correct_at1: bool | None = None
    correct_at3: bool | None = None
    abstained: bool | None = None
    path: Literal["memory_hit", "memory_miss"] | None = None
    reused_runbook_id: str | None = None  # set when path == memory_hit

    detection_time_ms: float | None = None
    detection_error_ms: float | None = None
    time_to_answer_ms: float | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    baseline_answers: dict = field(default_factory=dict)


@dataclass
class RemediatorOutput:
    """Contract #3. `path` is the field the Correlator's gating and the
    Grader's `source` both key off.
    """

    path: Literal["memory_hit", "memory_miss"]
    runbook: dict | None
    candidates: list[dict]
    ruled_out: list[RuledOut]
    confirm: dict  # {"match": bool, "confidence": float}
    timings_ms: dict  # {"stage0": float, "stage1": float, "stage2": float}
