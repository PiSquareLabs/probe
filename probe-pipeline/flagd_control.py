"""Shared flagd control: read/set the demo's fault-injection flags, and
the (on-variant, target service) each one needs. One source of truth for
this mapping, used by both serve_dashboard.py's REST endpoints and
pipeline_validation_run.py's CLI -- previously each script that needed
this list (validate_against_demo.py in several detector folders,
pipeline_validation_run.py) kept its own independent copy.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Repo-relative default (this checkout's own opentelemetry-demo), overridable
# via FLAGD_PATH for a machine where that demo stack lives somewhere else.
FLAGD_PATH = Path(os.environ.get("FLAGD_PATH", str(REPO_ROOT / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json")))

# flag -> (on-variant, target service or None -- kafkaQueueProblems has no
# direct trace/metric target, see DETECTORS.md)
FLAGS: dict[str, tuple[str, str | None]] = {
    "adFailure": ("on", "ad"),
    "adHighCpu": ("on", "ad"),
    "adManualGc": ("on", "ad"),
    "cartFailure": ("100%", "cart"),
    "paymentFailure": ("100%", "payment"),
    "recommendationCacheFailure": ("on", "recommendation"),
    "imageSlowLoad": ("10sec", "frontend"),
    "intlShippingSlowdown": ("10sec", "shipping"),
    "productCatalogFailure": ("on", "product-catalog"),
    "emailMemoryLeak": ("10x", "email"),
    "kafkaQueueProblems": ("on", None),
}


def _load() -> dict:
    return json.loads(FLAGD_PATH.read_text())


def _save(d: dict) -> None:
    FLAGD_PATH.write_text(json.dumps(d, indent=2))


def read_flags() -> dict:
    """Current defaultVariant for every known flag, plus its on-variant
    and target service, for the dashboard to render state + labels."""
    d = _load()
    out = {}
    for name, (on_variant, target) in FLAGS.items():
        current = d["flags"].get(name, {}).get("defaultVariant", "?")
        out[name] = {"current": current, "on_variant": on_variant,
                     "target_service": target, "is_on": current == on_variant}
    return out


def set_flag(name: str, variant: str) -> None:
    if name not in FLAGS:
        raise ValueError(f"unknown flag {name!r}")
    d = _load()
    if name not in d["flags"]:
        raise ValueError(f"{name!r} not present in {FLAGD_PATH}")
    d["flags"][name]["defaultVariant"] = variant
    _save(d)


def reset_all() -> list[str]:
    """Turn every known fault flag off; returns the ones that were on."""
    d = _load()
    changed = []
    for name in FLAGS:
        if name in d["flags"] and d["flags"][name]["defaultVariant"] != "off":
            d["flags"][name]["defaultVariant"] = "off"
            changed.append(name)
    if changed:
        _save(d)
    return changed


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 1:
        for name, info in read_flags().items():
            marker = "ON " if info["is_on"] else "off"
            print(f"[{marker}] {name:28s} current={info['current']:8s} target={info['target_service']}")
    elif sys.argv[1] == "--reset":
        changed = reset_all()
        print(f"reset: {changed or 'nothing was on'}")
    else:
        name = sys.argv[1]
        variant = sys.argv[2] if len(sys.argv) > 2 else FLAGS[name][0]
        set_flag(name, variant)
        print(f"{name} -> {variant}")
