#!/usr/bin/env python3
"""Adversarial cases — find where PROBE ACTUALLY fails, so the demo's honest
failure case is grounded in data rather than invented."""
from probe_sim import run
from generate_traces import Fault

CASES = [
 # name, expected root (None = PROBE should not confidently name anything), faults
 ("weak signal: cart 5% errors",        "cart",
   [Fault("cart","error",0.05,600)]),
 ("very weak: cart 2% errors",          "cart",
   [Fault("cart","error",0.02,600)]),
 ("BACKGROUND NOISE: payment fault + unrelated currency flap", "payment",
   [Fault("payment","error",0.80,600), Fault("currency","error",0.25,300)]),
 ("TWO ROOTS, SAME SUBTREE: payment + cart",  None,
   [Fault("payment","error",0.70,600), Fault("cart","error",0.70,602)]),
 ("SIMULTANEOUS, SAME BUCKET: ad + shipping at t=600", None,
   [Fault("ad","error",0.60,600), Fault("shipping","latency",4.5,600)]),
 ("LATE FAULT: only 6 buckets of post-change data", "payment",
   [Fault("payment","error",0.80,1140)]),
 ("SLOW BURN: gradual, no step", "email",
   [Fault("email","latency",1.4,600)]),
]

print("="*82); print("HARD MODE — where does PROBE actually break?"); print("="*82)
for name, expected, faults in CASES:
    print(f"\n▸ {name}")
    outcomes=[]
    for sd in (7,13,29,42,101):
        inc,a = run(faults, seed=sd)
        committed=[i for i in inc if not i["escalated"]]
        roots=[i["root"] for i in committed]
        if not inc: outcomes.append("NO DETECTION")
        elif not committed: outcomes.append("escalated")
        elif expected and roots and roots[0]==expected: outcomes.append("correct")
        elif expected is None: outcomes.append(f"named:{sorted(roots)}")
        else: outcomes.append(f"WRONG:{roots}")
    from collections import Counter
    for k,v in Counter(outcomes).most_common():
        print(f"    {v}/5  {k}")
