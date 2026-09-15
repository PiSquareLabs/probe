#!/usr/bin/env python3
"""
PROBE — synthetic EDOT-shaped trace generator for the Astronomy Shop.

Purpose: prove the PROBE ALGORITHM end-to-end without a cluster. Emits rows in
exactly the shape 01/02-detect-*.esql consume:
    (@timestamp, svc, is_error, duration_ns, kind)

HONESTY: this validates PROBE's LOGIC (bucketing -> detection -> sink ranking ->
splitting -> grading). It does NOT validate ES|QL syntax, and its detector is a
stand-in for Elasticsearch's CHANGE_POINT, not a reimplementation of it.
See research/12-SIMULATION-RESULTS.md for exactly what this does and does not prove.
"""
import random, math
from dataclasses import dataclass

# caller -> callee ("caller DEPENDS ON callee"; failure propagates callee -> caller)
EDGES = [
    ("frontend-proxy","frontend"),("load-generator","frontend-proxy"),
    ("frontend","ad"),("frontend","cart"),("frontend","checkout"),
    ("frontend","currency"),("frontend","product-catalog"),
    ("frontend","recommendation"),("frontend","image-provider"),
    ("checkout","cart"),("checkout","currency"),("checkout","email"),
    ("checkout","payment"),("checkout","product-catalog"),
    ("checkout","shipping"),("checkout","kafka"),
    ("recommendation","product-catalog"),("shipping","quote"),
    ("cart","valkey-cart"),
    ("accounting","kafka"),("fraud-detection","kafka"),
]
SERVICES = sorted({s for e in EDGES for s in e})

CALLERS = {}
for c, e in EDGES:
    CALLERS.setdefault(e, []).append(c)   # who depends on this service

# Baseline behaviour per service: (requests/sec, base error rate, base p95 ms)
BASE = {s: (random.uniform(8, 30), random.uniform(0.001, 0.02), random.uniform(20, 120))
        for s in SERVICES}
BASE["frontend"]       = (60, 0.005, 90)
BASE["product-catalog"]= (45, 0.004, 40)
BASE["checkout"]       = (25, 0.006, 150)

@dataclass
class Fault:
    service: str          # where the fault physically is
    kind: str             # "error" | "latency"
    magnitude: float      # target error rate, or latency multiplier
    start_s: int
    propagation_s: float = 0.0   # 0.0 = synchronous (same instant)

def ancestors(svc, depth=3):
    """Everything that transitively depends on svc (its blast radius)."""
    out, frontier = set(), {svc}
    for _ in range(depth):
        nxt = set()
        for n in frontier:
            nxt |= set(CALLERS.get(n, []))
        nxt -= out | {svc}
        if not nxt: break
        out |= nxt; frontier = nxt
    return out

def generate(duration_s=1200, bucket_s=10, faults=(), seed=7):
    """Yield (t_bucket, svc, errors, calls, p95_ns) per bucket per service."""
    rnd = random.Random(seed)
    rows = []
    for t in range(0, duration_s, bucket_s):
        for svc in SERVICES:
            rps, base_err, base_p95 = BASE[svc]
            calls = max(1, int(rnd.gauss(rps * bucket_s, rps * bucket_s * 0.15)))
            err_rate, p95 = base_err, base_p95

            for f in faults:
                # the faulty service itself
                if svc == f.service and t >= f.start_s:
                    if f.kind == "error":
                        err_rate = max(err_rate, f.magnitude)
                    else:
                        p95 = base_p95 * f.magnitude
                # victims: everything that transitively depends on it
                elif svc in ancestors(f.service) and t >= f.start_s + f.propagation_s:
                    # attenuates with distance: a caller only fails on the
                    # fraction of its requests that touch the broken path
                    share = rnd.uniform(0.35, 0.8)
                    if f.kind == "error":
                        err_rate = max(err_rate, f.magnitude * share)
                    else:
                        p95 = max(p95, base_p95 * (1 + (f.magnitude - 1) * share))

            err_rate = min(0.99, max(0.0, rnd.gauss(err_rate, err_rate * 0.12 + 0.002)))
            errors = sum(1 for _ in range(calls) if rnd.random() < err_rate)
            p95_ns = int(max(1.0, rnd.gauss(p95, p95 * 0.10)) * 1_000_000)  # ms -> NANOS
            rows.append((t, svc, errors, calls, p95_ns))
    return rows

if __name__ == "__main__":
    rows = generate(faults=[Fault("product-catalog", "error", 0.55, 600)])
    print(f"services={len(SERVICES)} rows={len(rows)} buckets={len(rows)//len(SERVICES)}")
    print("sample (t, svc, errors, calls, p95_ns):")
    for r in rows[:3]: print("  ", r)
    print("blast radius of product-catalog:", sorted(ancestors("product-catalog")))
    print("blast radius of kafka:          ", sorted(ancestors("kafka")))
