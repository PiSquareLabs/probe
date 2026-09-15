#!/usr/bin/env python3
"""
PROBE — offline simulation of the full pipeline.
Mirrors 01/02-detect-*.esql -> 03-causal-rank.esql -> 04-split.esql -> 06-self-grade.esql
"""
import statistics as st
from generate_traces import generate, Fault, EDGES, SERVICES

DEPS = {}
for c, e in EDGES:
    DEPS.setdefault(c, set()).add(e)

# ---------------------------------------------------------------- detection
def change_point(series, min_points=22):
    """
    Stand-in for ES|QL CHANGE_POINT (step_change). Welch t-test over every split;
    returns (index, pseudo_pvalue) of the most significant step, or None.

    NOT a reimplementation of Elasticsearch's detector — it exists to prove the
    PIPELINE, and it is deliberately WEAKER than the real thing (no distribution
    or trend changes), so results here are a LOWER bound.
    """
    n = len(series)
    if n < min_points:
        return None                      # mirrors the documented >=22 floor [F1]
    best = None
    for i in range(8, n - 8):
        a, b = series[:i], series[i:]
        ma, mb = st.fmean(a), st.fmean(b)
        va, vb = st.pvariance(a), st.pvariance(b)
        se = math_sqrt(va / len(a) + vb / len(b))
        if se <= 1e-12:
            continue
        t = abs(mb - ma) / se
        if best is None or t > best[1]:
            best = (i, t)
    if not best or best[1] < 6.0:        # significance floor
        return None
    idx, t = best
    return idx, max(1e-300, 2.718 ** (-t))   # monotone decreasing in t, like a p-value

def math_sqrt(x):
    return x ** 0.5

def detect(rows, bucket_s=10, metric="error"):
    by = {}
    for t, svc, errors, calls, p95_ns in rows:
        v = (errors / calls) if metric == "error" else (p95_ns / 1e6)
        by.setdefault(svc, []).append((t, v, calls))
    out = {}
    for svc, pts in by.items():
        pts.sort()
        if any(c < 5 for _, _, c in pts):
            pts = [(t, v, c) for t, v, c in pts if c >= 5]   # mirrors WHERE calls >= 5
        series = [v for _, v, _ in pts]
        cp = change_point(series)
        if cp:
            idx, p = cp
            out[svc] = {"change_ts": pts[idx][0], "pvalue": p, "signal": metric}
    return out

# ------------------------------------------------------- causal rank + split
def rank_and_split(anoms, bucket_s=10):
    """03-causal-rank.esql + 04-split-concurrent-incidents.esql"""
    aset = set(anoms)
    roots = []
    for svc in sorted(aset):
        anomalous_deps = DEPS.get(svc, set()) & aset
        if not anomalous_deps:                       # WHERE is_sink == true
            roots.append(svc)
    roots.sort(key=lambda s: (anoms[s]["change_ts"], anoms[s]["pvalue"]))

    def reaches(a, target, depth=4):
        frontier = {a}
        for _ in range(depth):
            nxt = set()
            for n in frontier:
                nxt |= DEPS.get(n, set())
            if target in nxt: return True
            frontier = nxt
        return False

    incidents = []
    for r in roots:
        blast = {r} | {s for s in aset
                       if s != r and reaches(s, r)
                       and anoms[s]["change_ts"] >= anoms[r]["change_ts"] - 3 * bucket_s}
        incidents.append({"root": r, "change_ts": anoms[r]["change_ts"],
                          "blast": sorted(blast), "size": len(blast)})
    return incidents

# ------------------------------------------------------------- confidence
def confidence(incidents, anoms, total_services, bucket_s=10):
    """
    The escalation rule from 03-MECHANISM-DESIGN.md §5.

    ⚠ BUG FOUND BY SIMULATION: the original rule escalated whenever >40% of
    services were anomalous. But N genuine concurrent incidents LEGITIMATELY make
    many services anomalous — so the rule fired on exactly the case PROBE exists
    to handle, escalating all three incidents in the flagship demo instead of
    reporting them.

    FIX: headcount alone does not indicate a systemic event. What does is
    UNEXPLAINED anomaly. If the identified roots' blast radii COVER the anomalous
    set, the split is trustworthy no matter how many services are involved.
    Escalate only when coverage is poor (something is broken that no root
    explains) or when the root count is implausibly high.
    """
    covered = set()
    for inc in incidents:
        covered |= set(inc["blast"])
    coverage = len(covered & set(anoms)) / max(1, len(anoms))

    out = []
    for i, inc in enumerate(incidents):
        reasons, conf = [], 0.92
        if coverage < 0.80:
            conf = 0.45
            reasons.append(f"only {coverage:.0%} of anomalies explained by any root "
                           f"— unexplained breakage, likely infra-wide")
        elif len(incidents) > 5:
            conf = 0.50
            reasons.append(f"{len(incidents)} simultaneous roots — implausible as "
                           f"independent incidents; likely systemic")
        others = [x for x in incidents if x is not inc]
        if others:
            margin = min(abs(inc["change_ts"] - o["change_ts"]) for o in others)
            if margin < bucket_s:
                conf = min(conf, 0.58)
                reasons.append(f"margin {margin}s < 1 bucket — order not resolvable")
        if inc["size"] == 1:
            conf = min(conf, 0.60); reasons.append("nothing propagated — possibly noise")
        out.append({**inc, "confidence": round(conf, 2),
                    "escalated": conf < 0.70, "reasons": reasons})
    return out

def run(faults, duration_s=1200, bucket_s=10, seed=7, use_latency=True):
    rows = generate(duration_s, bucket_s, faults, seed)
    a = detect(rows, bucket_s, "error")
    if use_latency:
        for svc, d in detect(rows, bucket_s, "latency").items():
            if svc not in a or d["pvalue"] < a[svc]["pvalue"]:
                a[svc] = d
    inc = rank_and_split(a, bucket_s)
    return confidence(inc, a, len(SERVICES), bucket_s), a
