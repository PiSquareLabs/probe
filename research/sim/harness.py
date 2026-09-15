#!/usr/bin/env python3
"""PROBE self-grading harness, run offline against synthetic traces."""
from probe_sim import run
from generate_traces import Fault

# (flag, expected_root, fault) — matches research/artifacts/ground-truth.ndjson
SINGLE = [
 ("adFailure",                   "ad",              Fault("ad","error",0.60,600)),
 ("adHighCpu",                   "ad",              Fault("ad","latency",4.0,600)),
 ("cartFailure(100%)",           "cart",            Fault("cart","error",0.70,600)),
 ("cartFailure(10%)",            "cart",            Fault("cart","error",0.10,600)),
 ("emailMemoryLeak",             "email",           Fault("email","latency",3.5,600)),
 ("failedReadinessProbe",        "cart",            Fault("cart","error",0.50,600)),
 ("imageSlowLoad",               "image-provider",  Fault("image-provider","latency",5.0,600)),
 ("intlShippingSlowdown",        "shipping",        Fault("shipping","latency",4.5,600)),
 ("kafkaQueueProblems",          "kafka",           Fault("kafka","latency",3.0,600,propagation_s=20)),
 ("paymentFailure(100%)",        "payment",         Fault("payment","error",0.80,600)),
 ("paymentUnreachable",          "payment",         Fault("payment","error",0.95,600)),
 ("productCatalogFailure",       "product-catalog", Fault("product-catalog","error",0.55,600)),
 ("productCatalogLockContention","product-catalog", Fault("product-catalog","latency",4.0,600)),
 ("recommendationCacheFailure",  "recommendation",  Fault("recommendation","latency",3.5,600)),
]
CONCURRENT = [
 ("payment + recommendation + kafka", {"payment","recommendation","kafka"},
  [Fault("payment","error",0.80,600),
   Fault("recommendation","latency",3.5,610),
   Fault("kafka","latency",3.0,620,propagation_s=20)]),
 ("ad + shipping", {"ad","shipping"},
  [Fault("ad","error",0.60,600), Fault("shipping","latency",4.5,640)]),
]

def grade(seeds=(7,13,29,42,101), use_latency=True):
    correct=incorrect=escalated=0; detail=[]
    for name, expected, f in SINGLE:
        hits=[]
        for sd in seeds:
            inc,_ = run([f], seed=sd, use_latency=use_latency)
            committed=[i for i in inc if not i["escalated"]]
            if not inc:                       hits.append("miss")
            elif not committed:               hits.append("escalated")
            elif committed[0]["root"]==expected: hits.append("correct")
            else:                             hits.append(f"WRONG:{committed[0]['root']}")
        c=hits.count("correct"); e=hits.count("escalated"); w=len(hits)-c-e
        correct+=c; escalated+=e; incorrect+=w
        detail.append((name,expected,c,e,w,hits))
    return correct,incorrect,escalated,detail

if __name__=="__main__":
    seeds=(7,13,29,42,101)
    print("="*78); print("PROBE SELF-GRADING HARNESS — synthetic traces, 5 seeds per fault")
    print("="*78)
    c,w,e,detail = grade(seeds)
    print(f"\n{'fault':30} {'expected root':17} {'ok':>3} {'esc':>4} {'wrong':>6}")
    print("-"*78)
    for name,exp,cc,ee,ww,hits in detail:
        flag = "  " if ww==0 else " ❌"
        print(f"{name:30} {exp:17} {cc:>3} {ee:>4} {ww:>6}{flag}")
    tot=c+w+e
    print("-"*78)
    print(f"{'TOTAL':30} {'':17} {c:>3} {e:>4} {w:>6}   (n={tot})")
    print(f"\ncorrect {c}/{tot}   escalated {e}/{tot}   WRONG {w}/{tot}")
    print(f"precision when committed: {c}/{c+w} = {100.0*c/max(1,c+w):.1f}%")

    print("\n"+"="*78); print("CONCURRENT UNRELATED INCIDENTS"); print("="*78)
    for name, expected, faults in CONCURRENT:
        inc,_ = run(faults, seed=7)
        roots={i["root"] for i in inc}
        ok = roots==expected
        print(f"\n{name}\n  expected roots: {sorted(expected)}")
        print(f"  PROBE returned: {sorted(roots)}  -> {'✅ CORRECT' if ok else '❌ WRONG'}")
        for i in inc:
            print(f"     incident root={i['root']:16} conf={i['confidence']} "
                  f"esc={i['escalated']} blast={i['size']} {i['blast']}")
