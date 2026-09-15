#!/usr/bin/env python3
"""How many buckets AFTER a change does PROBE need before it can name the root?
This converts the [REASONING] latency budget in 07-ARCHITECTURE-SPEED.md into a
measured number (on synthetic data)."""
from probe_sim import run
from generate_traces import Fault

BUCKET_S=10; DURATION=1200
print("="*76)
print("DETECTION LATENCY — post-change buckets required to name the root")
print("="*76)
print(f"{'fault':34} {'buckets':>8} {'seconds':>8}  (median of 5 seeds)")
print("-"*76)
rows=[]
for name, svc, kind, mag in [
    ("payment 80% errors",        "payment","error",0.80),
    ("product-catalog 55% errors","product-catalog","error",0.55),
    ("cart 10% errors",           "cart","error",0.10),
    ("cart 5% errors",            "cart","error",0.05),
    ("ad latency x4",             "ad","latency",4.0),
    ("shipping latency x4.5",     "shipping","latency",4.5),
]:
    per_seed=[]
    for sd in (7,13,29,42,101):
        found=None
        for post in range(3, 30):                 # post-change buckets available
            start = DURATION - post*BUCKET_S
            inc,_ = run([Fault(svc,kind,mag,start)], DURATION, BUCKET_S, sd)
            committed=[i for i in inc if not i["escalated"]]
            if committed and committed[0]["root"]==svc:
                found=post; break
        per_seed.append(found if found else 99)
    per_seed.sort(); med=per_seed[len(per_seed)//2]
    rows.append((name,med))
    disp = f"{med}" if med<99 else ">29"
    secs = f"{med*BUCKET_S}s" if med<99 else "—"
    print(f"{name:34} {disp:>8} {secs:>8}")
print("-"*76)
ok=[m for _,m in rows if m<99]
if ok:
    print(f"\nrange: {min(ok)}–{max(ok)} buckets = {min(ok)*BUCKET_S}–{max(ok)*BUCKET_S}s "
          f"of post-change data at a {BUCKET_S}s bucket")
    print(f"total recognition latency = ingest lag (2-7s) + above")
