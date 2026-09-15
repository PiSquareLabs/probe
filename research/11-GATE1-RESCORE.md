> Simulated panel. Role-based lenses from public professional profiles — not the actual
> judges' views or scores.

# Gate 1 — RE-SCORE after all fixes
**73.5 → 81.0 / 100** · **Top-3 contender band. No dead criterion.** Target (75+) cleared.

## Scorecard delta

| Criterion | Wt | Was | Now | What moved it |
|---|---|---|---|---|
| Originality | 10 | 7 | **7.5** | Splitting validated across 11 scenarios; hub-failure insight is demonstrably real, not asserted |
| AI Implementation | 15 | 12 | **13** | Cost sourced; uncertainty rule sharpened; eval harness explicitly load-bearing |
| Elasticsearch Integration | 15 | 12.5 | **12.5** | ⚠ **unchanged — only execution moves this** |
| Technology Stack | 15 | 11 | **12** | Sourced cost, documented degradation modes, rate-limit answer |
| Problem Solving | 10 | 8.5 | **9** | MTTR claim replaced with DORA + measured result |
| Market Potential | 10 | 5 | **8** | ☠→✅ Named buyer, budget line, wedge, honest ceiling, clear ask |
| Interface Design | 8 | 4.5 | **4.5** | ⚠ **unchanged — the one remaining gap** |
| Usability | 7 | 5 | **5.5** | Escalation behaviour explicit; user knows what to do when unsure |
| Demo Quality | 5 | 4 | **4.5** | "Pick a flag" + honest failure case; 5 needs it live and rehearsed |
| Pitch Effectiveness | 5 | 4 | **4.5** | 30-second insight-first opening; clear ask |
| **Total** | **100** | **73.5** | **81.0** | |

## What's still on the table — and it is now only two things

| Gap | Points | Cost | Verdict |
|---|---|---|---|
| **The Incident Card dashboard** | +2.5 (Interface 4.5→6.5, Usability +0.5) | 1h | **Highest remaining value.** Cut the latency query before cutting this. |
| **Actually running the queries** | +3.0 (ES Integration 12.5→14, Demo 4.5→5) | in-event | This is what H0:45–H5 exists for |

**Ceiling if both land: ~86.5.** That is the winning band, and it requires **zero new design
work** — only execution.

## What the panel would now say

**Ramnani:** "Same verdict on depth — it's the best Elastic usage I've read. The difference is
they now know their own failure modes, including the ones that fail silently. I still want to
see it run."

**Pendyala:** "The cost number is real and the degradation modes are thought through. The
answer on rate limits — that the LLM is off the critical path so a limit delays explanations
and not detection — is the correct architectural answer and most teams can't give it."

**Bose:** "They fixed the number I objected to by replacing it with a measurement, and they
refuse credit for lucky guesses. I'd still like to see one screen an SRE lead can read."

**Virk:** "They told me the ceiling instead of inflating it, and named the buyer and the
budget line. Conceding that this is a wedge into the Elastic base rather than a platform is
what makes me believe the rest."

## The one thing that would most change the outcome
Not more design. **Get `03-causal-rank.esql` returning a correct root cause on a live
cluster.** Every remaining deduction across Elasticsearch Integration, Demo Quality and Tech
Stack traces to the same root: nothing has executed.
