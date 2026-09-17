"""Reads a runbook out of the local `catalog/*.yaml` files -- the 13
flag write-ups from earlier in this project (cause/steps/permanent_fix
per fault, keyed by flag name) -- indexed by `(fault_class, service)`,
the same key `probe-memory` runbook docs use.

This is a local, no-Elasticsearch-required stand-in, for now: `catalog/`
already has hand-written, reviewed root causes and steps for every flag
that causes a failure, so it's a ready-made source of runbook content
before `probe-memory` (setup_probe_memory.py) or the GitHub PR mirror
(github_runbook.py, writer.py) are actually live and populated.

Same "off the graded path" status as github_runbook.py: nothing in
remediator.py or correlator.py imports this module. The Remediator's
stage 0/1/ruled-out reads only ever hit `probe-memory`
(PROBE-component-contracts.md #7a) -- this is for a human, a script, or
a future Writer-seeding step to read from directly, not for the graded
retrieval loop to fall back on.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = REPO_ROOT / "catalog"


@dataclass
class CatalogRunbook:
    flag: str
    fault_class: str
    service: str
    cause: str
    steps: list[str]
    permanent_fix: str
    notes: str
    enabled_value: str
    drift: bool
    source: str = "catalog"


def _load_all(catalog_dir: Path) -> dict[tuple[str, str], CatalogRunbook]:
    by_key: dict[tuple[str, str], CatalogRunbook] = {}
    for path in sorted(catalog_dir.glob("*.yaml")):
        entry = yaml.safe_load(path.read_text(encoding="utf-8"))
        truth = entry["truth"]
        runbook = entry["reference_runbook"]
        key = (truth["fault_class"], truth["service"])
        by_key[key] = CatalogRunbook(
            flag=entry["flag"],
            fault_class=truth["fault_class"],
            service=truth["service"],
            cause=runbook["cause"],
            steps=list(runbook["steps"]),
            permanent_fix=runbook["permanent_fix"],
            notes=entry.get("notes", ""),
            enabled_value=entry["enabled_value"],
            drift=entry["drift"],
        )
    return by_key


# Loaded once per process; catalog/*.yaml is static hand-written content,
# not something that changes mid-run the way probe-memory does.
_INDEX: dict[tuple[str, str], CatalogRunbook] | None = None


def _index(catalog_dir: Path = CATALOG_DIR) -> dict[tuple[str, str], CatalogRunbook]:
    global _INDEX
    if _INDEX is None:
        _INDEX = _load_all(catalog_dir)
    return _INDEX


def read_local(fault_class: str, service: str, catalog_dir: Path = CATALOG_DIR) -> CatalogRunbook | None:
    """Looks up catalog/*.yaml by (fault_class, service) -- the same
    key a `probe-memory` runbook doc's id uses
    (writer.py's `_runbook_id()`). Returns None if no catalog entry
    matches (e.g. `kafkaQueueProblems`'s fault_class/service pair
    doesn't correspond 1:1 the way most of the 13 do, or a fault the
    pipeline names doesn't have a hand-written catalog entry at all).
    """
    return _index(catalog_dir).get((fault_class, service))


def read_by_flag(flag: str, catalog_dir: Path = CATALOG_DIR) -> CatalogRunbook | None:
    """Convenience lookup by the original flag name instead of
    (fault_class, service) -- useful for the validate_against_demo.py
    style scripts that already know which flag they injected.
    """
    for runbook in _index(catalog_dir).values():
        if runbook.flag == flag:
            return runbook
    return None


def all_runbooks(catalog_dir: Path = CATALOG_DIR) -> list[CatalogRunbook]:
    return list(_index(catalog_dir).values())
