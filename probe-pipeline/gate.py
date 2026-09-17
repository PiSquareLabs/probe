"""Gate -- "should anyone care?" (PROBE-component-contracts.md #2).

In:  Detector output + this module's own persistent transient-counter state.
Out: Decision.
Reads nothing from Elasticsearch. Uses no LLM, no graph.

The only module besides the harness with cross-scan state: transient
counters live for the process lifetime, keyed by (service, signal).

Never: runs anything beyond the harness's own probe-runs bookkeeping on
a watch/transient result (that bookkeeping is the harness's job, not
this module's -- gate() has no Elasticsearch write path at all); opens
an incident on a resource-only anomaly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from schemas import Candidate, Decision

# Symptom signals can open an incident on their own. Resource signals
# (contract #2) never can -- they only ever ride along as
# supporting_evidence on an incident some symptom signal already opened.
# Names match the real Detector's signals
# (probe-two-tier-detector-v3/zscore_scan.py) -- "log_error_count", not
# the "log_errors" this file guessed before that Detector existed.
SYMPTOM_SIGNALS = frozenset({"p95_latency", "error_rate"})
RESOURCE_SIGNALS = frozenset({"cpu", "memory", "log_error_count"})

# "spike" recovers on its own (transient); anything else that CHANGE_POINT
# confirmed (step_change, dip, distribution_change, trend_change) is read
# as a lasting shift (sustained).
RECOVERING_TYPES = frozenset({"spike"})

TRANSIENT_WINDOW_SECONDS = 10 * 60
INTERMITTENT_THRESHOLD = 3


@dataclass
class _TransientCounter:
    timestamps: list[float] = field(default_factory=list)

    def bump(self, now: float) -> int:
        self.timestamps = [t for t in self.timestamps if now - t < TRANSIENT_WINDOW_SECONDS]
        self.timestamps.append(now)
        return len(self.timestamps)


class Gate:
    """Holds the transient counters. One instance per running harness
    process -- that's the "process lifetime" the contract means.
    """

    def __init__(self) -> None:
        self._transient_counts: dict[tuple[str, str], _TransientCounter] = {}

    def _is_symptom(self, c: Candidate) -> bool:
        return c.tier == 2 and c.signal in SYMPTOM_SIGNALS and c.type is not None

    def _bump_transient(self, candidate: Candidate, now: float) -> int:
        key = (candidate.service, candidate.signal)
        counter = self._transient_counts.setdefault(key, _TransientCounter())
        return counter.bump(now)

    def evaluate(self, detector_output: dict | None, now: float | None = None) -> Decision:
        """detector_output is the Detector's `{candidates, loudest,
        earliest, scan_at}` shape, or None on an empty scan.
        """
        if now is None:
            now = time.time()
        if not detector_output or not detector_output.get("candidates"):
            return Decision(kind="watch", pattern=None, trigger=[])

        candidates: list[Candidate] = detector_output["candidates"]
        symptom_candidates = [c for c in candidates if self._is_symptom(c)]
        resource_candidates = [c for c in candidates if c.tier == 2 and c.signal in RESOURCE_SIGNALS]

        if not symptom_candidates:
            # Resource-only anomalies never open an incident on their own.
            return Decision(kind="watch", pattern=None, trigger=[])

        for candidate in symptom_candidates:
            if candidate.type not in RECOVERING_TYPES:
                # Tier-2 symptom, not recovered -> incident/sustained, immediately.
                return Decision(
                    kind="incident",
                    pattern="sustained",
                    trigger=[candidate],
                    supporting_evidence=resource_candidates,
                    evidence_timestamps=[c.timestamp for c in resource_candidates if c.timestamp],
                )

        # Every symptom candidate this scan recovered on its own (spike) --
        # each is transient. Bump its counter; the third in the window
        # upgrades that (service, signal) pair to incident/intermittent.
        escalated: list[Candidate] = []
        for candidate in symptom_candidates:
            count = self._bump_transient(candidate, now)
            if count >= INTERMITTENT_THRESHOLD:
                escalated.append(candidate)

        if escalated:
            return Decision(
                kind="incident",
                pattern="intermittent",
                trigger=escalated,
                supporting_evidence=resource_candidates,
                evidence_timestamps=[c.timestamp for c in resource_candidates if c.timestamp],
            )

        return Decision(kind="transient", pattern=None, trigger=symptom_candidates)
