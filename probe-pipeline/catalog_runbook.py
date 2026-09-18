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


def _load_all(catalog_dir: Path) -> tuple[dict[tuple[str, str], list[CatalogRunbook]], dict[str, CatalogRunbook]]:
    """Two indices from one pass: by (fault_class, service) -- the same
    key a `probe-memory` runbook doc's id uses (writer.py's
    `_runbook_id()`) -- and by flag name, which is always unique (one
    catalog file per flag).

    by_key maps to a LIST, not a single entry: `adHighCpu` and
    `adManualGc` both genuinely key to (resource_exhaustion, ad), and an
    earlier version of this function silently let the
    alphabetically-later file overwrite the earlier one in the index,
    losing adHighCpu entirely (surfaced live when watch_pipeline.py
    couldn't find its catalog entry). A collision here is real data,
    the same way PROBE-runbook-search-cases.md's Case H describes two
    runbooks legitimately sharing one fingerprint -- the fix is to keep
    both and let the caller disambiguate, not to silently pick a winner.
    """
    by_key: dict[tuple[str, str], list[CatalogRunbook]] = {}
    by_flag: dict[str, CatalogRunbook] = {}
    for path in sorted(catalog_dir.glob("*.yaml")):
        entry = yaml.safe_load(path.read_text(encoding="utf-8"))
        truth = entry["truth"]
        runbook = entry["reference_runbook"]
        key = (truth["fault_class"], truth["service"])
        parsed = CatalogRunbook(
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
        by_key.setdefault(key, []).append(parsed)
        by_flag[parsed.flag] = parsed
    return by_key, by_flag


# Loaded once per process; catalog/*.yaml is static hand-written content,
# not something that changes mid-run the way probe-memory does.
_INDEX: dict[tuple[str, str], list[CatalogRunbook]] | None = None
_BY_FLAG: dict[str, CatalogRunbook] | None = None


def _index(catalog_dir: Path = CATALOG_DIR) -> dict[tuple[str, str], list[CatalogRunbook]]:
    global _INDEX, _BY_FLAG
    if _INDEX is None:
        _INDEX, _BY_FLAG = _load_all(catalog_dir)
    return _INDEX


def _by_flag_index(catalog_dir: Path = CATALOG_DIR) -> dict[str, CatalogRunbook]:
    global _INDEX, _BY_FLAG
    if _BY_FLAG is None:
        _INDEX, _BY_FLAG = _load_all(catalog_dir)
    return _BY_FLAG


def read_local_all(fault_class: str, service: str, catalog_dir: Path = CATALOG_DIR) -> list[CatalogRunbook]:
    """Every catalog entry matching (fault_class, service) -- usually
    exactly one, but genuinely two for (resource_exhaustion, ad)
    (adHighCpu and adManualGc). Empty list if none match. This is the
    source of truth; read_local() is a convenience wrapper over it.
    """
    return list(_index(catalog_dir).get((fault_class, service), []))


def read_local(fault_class: str, service: str, catalog_dir: Path = CATALOG_DIR) -> CatalogRunbook | None:
    """Looks up catalog/*.yaml by (fault_class, service). Returns None
    if nothing matches. If more than one entry matches (the
    adHighCpu/adManualGc collision), this raises rather than silently
    picking one -- call read_local_all() instead when you know the key
    might be ambiguous, or read_by_flag() when you know the flag name
    (never ambiguous).
    """
    matches = read_local_all(fault_class, service, catalog_dir)
    if len(matches) > 1:
        flags = [m.flag for m in matches]
        raise ValueError(
            f"({fault_class!r}, {service!r}) matches {len(matches)} catalog entries {flags} -- "
            "ambiguous, use read_local_all() or read_by_flag() instead"
        )
    return matches[0] if matches else None


def read_by_flag(flag: str, catalog_dir: Path = CATALOG_DIR) -> CatalogRunbook | None:
    """Convenience lookup by the original flag name instead of
    (fault_class, service) -- useful for the validate_against_demo.py
    style scripts that already know which flag they injected. Unlike
    read_local(), this is never affected by the (fault_class, service)
    key collision -- every flag has its own catalog file, so this
    always finds it if the file exists.
    """
    return _by_flag_index(catalog_dir).get(flag)


def all_runbooks(catalog_dir: Path = CATALOG_DIR) -> list[CatalogRunbook]:
    """All 13 catalog entries, one per flag -- not filtered through the
    (fault_class, service) index, so this doesn't lose adHighCpu the
    way an earlier version of this function did.
    """
    return list(_by_flag_index(catalog_dir).values())
