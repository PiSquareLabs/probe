"""Bridges the real Detector (../probe-two-tier-detector-v3) into
gate.py and correlator.py, which were written against schemas.Candidate
before that Detector existed and don't import it directly (per the
contract: "Each module is a file. None imports another's internals.").

Two adapter jobs:

  1. `scan()` -- runs v3's `detector_scan()` and converts its raw dict
     candidates into schemas.Candidate objects, so gate.py's attribute
     access (`c.service`, `c.tier`, ...) keeps working unchanged. Owns
     the `streak_state` dict's lifetime the same way gate.py owns its
     transient counters -- one instance per running harness process,
     passed back in on every call (Tier 1's persistence, C3, needs it
     to survive across scans; see v3/detector.py's own docstring).

  2. `change_point_for_correlator()` -- v3's `change_point(service, signal)`
     already returns exactly the `dict | None` shape correlator.py's
     `change_point_fn` parameter expects (contract #4 step 5's one
     extra hop), so this is a thin re-export, not a real adapter --
     it exists so correlator.py depends on a name inside probe-pipeline/,
     not on a sibling directory's import path.

This module is the one place that knows both directory layouts exist;
nothing else in probe-pipeline/ should import from
../probe-two-tier-detector-v3 directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

from schemas import Candidate

_V3_DIR = Path(__file__).resolve().parent.parent / "probe-two-tier-detector-v3"

# v3 has its own es_client.py/queries.py/zscore_scan.py/change_point.py/
# detector.py -- and this directory (probe-pipeline/) has an es_client.py
# too. Both use bare `import es_client`, and Python's module cache is
# keyed by that name alone, so a naive `sys.path.insert` here would make
# v3/zscore_scan.py's `from es_client import esql_rows` silently resolve
# to *our* es_client.py (no such function) once ours has been imported
# first -- wrong module, no error, just a confusing ImportError or,
# worse, a wrong-behaving es_client.esql call downstream.
#
# Fix: while importing v3's detector/change_point, evict any of v3's
# module names our own imports may have already cached, force Python to
# reload them fresh off v3's directory, then put our own cached modules
# back exactly as they were -- so every other file in probe-pipeline/
# that does `import es_client` afterward still gets *our* es_client.py,
# not v3's.
_V3_SHADOWED_NAMES = ("es_client", "queries", "zscore_scan", "change_point", "detector")


def _import_from_v3():
    saved = {name: sys.modules.pop(name, None) for name in _V3_SHADOWED_NAMES}
    sys.path.insert(0, str(_V3_DIR))
    try:
        from change_point import change_point as change_point_fn
        from detector import detector_scan as detector_scan_fn
        return change_point_fn, detector_scan_fn
    finally:
        sys.path.remove(str(_V3_DIR))
        for name in _V3_SHADOWED_NAMES:
            sys.modules.pop(name, None)  # drop v3's copy we just loaded
            if saved[name] is not None:
                sys.modules[name] = saved[name]  # restore probe-pipeline's own


_change_point, _detector_scan = _import_from_v3()

# The real Detector's signal names (probe-two-tier-detector-v3/zscore_scan.py).
# Notably "log_error_count", not "log_errors" -- gate.py's RESOURCE_SIGNALS
# has been corrected to match this, not the other way around.
SIGNAL_NAMES = frozenset({"p95_latency", "error_rate", "log_error_count", "cpu", "memory"})


def _to_candidate(raw: dict) -> Candidate:
    return Candidate(
        service=raw["service"],
        signal=raw["signal"],
        type=raw.get("type"),
        timestamp=raw.get("timestamp"),
        pvalue=raw.get("pvalue"),
        z=raw["z"],
        tier=raw["tier"],
        reason=raw.get("reason"),
        breaks=raw.get("breaks"),
    )


def scan(streak_state: dict, **kwargs) -> dict | None:
    """One real Detector scan, in the `{candidates, loudest, earliest,
    scan_at}` shape gate.py's `evaluate()` expects -- `candidates` as
    schemas.Candidate objects instead of v3's raw dicts.

    `streak_state` must be the same dict passed in on every call for
    Tier 1 persistence to mean anything (v3/detector.py: a fresh dict
    each call makes PERSISTENCE=2 never fire). Own one `{}` per running
    harness process and reuse it -- exactly like gate.py's own
    `_transient_counts`.
    """
    raw = _detector_scan(streak_state, **kwargs)
    if raw is None:
        return None
    return {
        "candidates": [_to_candidate(c) for c in raw["candidates"]],
        "loudest": raw["loudest"],
        "earliest": raw["earliest"],
        "scan_at": raw["scan_at"],
    }


def change_point_for_correlator(service: str, signal: str) -> dict | None:
    """Correlator's one extra hop (contract #4 step 5) -- same
    signature and return shape as v3's change_point() already:
    `{"type", "timestamp", "pvalue", "breaks"}` on a confirmed break,
    `{"type": None, ..., "reason": "insufficient_data"}` when the
    series was too sparse, or `None` on a genuine no-break.
    """
    return _change_point(service, signal)
