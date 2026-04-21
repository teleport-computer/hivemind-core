# Final learnings — Hivemind research phase

_Closing summary, 2026-04-21. Canonical list of what 51 iterations actually taught us.
For the long-form synthesis see `CONCLUSIONS.md`; for the running log see `findings.md`._

---

## Model and runtime

1. **Claw-code is basically the same as anthropic-sdk.** Score equivalent within ±3; claw
   has +40% wall time. Runtime choice is not a lever.

2. **Kimi is better than Haiku because it is more risk-seeking** — more willing to
   transform rather than refuse. But not uniformly: **Kimi's mediator generalizes
   better than Haiku's; Kimi's scope does not.** In scope-only config Kimi underperforms
   Haiku (iter53 C 76 vs iter49 C 83). Kimi's edge is concentrated at the mediator stage.

## Agent superpowers — what gets used

3. **Source-reading has no effect on frontier search.** Observed in 2 of 40 tapes; simulate
   subsumes its information content. *But may be useful for more advanced agents that
   reason about NPC behavior.*

4. **Simulation is only used once, not iteratively.** ~50% of runs invoke simulate_query,
   always exactly once, always post-verify, always as a sanity check before shipping.
   The save/load/revert metaphor is not being exercised. *But may be useful for more
   advanced agents.*

5. **Concurrent/parallel simulation is not used.** simulate_multi (iter45/46) was added,
   exposed as MCP, promoted into the canonical workflow — 0/11 calls. The model defaults
   to single simulate_query regardless of prompt surgery. *But may be useful for more
   advanced agents.*

6. **MCP is load-bearing; CLI/filesystem surface is essentially unused for its
   prompt-described purpose.** Scope never calls Bash/Read/Grep/Glob for SQL/verify/
   simulate — 100% MCP. *But may be useful for more advanced agents.*

## Reliability and correctness

7. **Verify-scope-function before output is definitely worth it.** The AST validator with
   never-deny rule is the single most impactful component we added. It fires in ~40% of
   runs, teaches the model mid-loop through specific error messages, and converts
   wrong-signature/deny-shaped emits to transform-shaped ones. +15 to +20 overall
   contribution; load-bearing.

8. **Retry-on-rejection is negative (iter40).** Scope's first emit is usually right; when
   it's wrong, the wrongness came from turn-1 strategy bias, and a retry inherits that
   bias rather than fixing it. `HIVEMIND_SCOPE_MAX_ATTEMPTS=1` shipped as default.

## Policy shape and model behavior

9. **Models tend to reject rather than transform**, but this is not the model optimizing
   "privacy over utility" — it's **row-exclusion policy wording** ("block content about X")
   pattern-matching the model's refusal prior. Refusal-by-pattern, not principled privacy
   reasoning. Evidence: the never-deny validator flips behavior without changing the
   prior — rejection was sloppy pattern-matching, not deliberate.

10. **Row-exclusion is architecturally harder than value-redaction.** Policies that ask
    for row *exclusion* (temporal windows, topic drops) reliably activate the refusal
    prior. Value redaction (PII scrubbing, credential stripping) cleanly hits transform
    mode. This gap is the ceiling on temporal_scoping and topic_filtering across every
    config we tried.

11. **Semantic-lift is a bimodal outcome: good on value-queries, bad on row-queries.**
    +20 on pii_redaction, −28 on topic_filtering when enabled. Same feature helps and
    hurts depending on policy shape.

## Architecture

12. **Scope and mediator both help, but scope consumes more tokens.** Scope runs 12–17
    LLM calls per invocation (validator iteration, SQL probes, simulate). Mediator runs
    a single turn. The cost ratio is roughly 15×.

13. **Scope and mediator gap-cover each other.** Single-component ablations (scope-only,
    mediator-only) scored within the same band as the full stack. Each layer catches a
    different fraction of attacks; removing one exposes the other's blind spots. Not
    clean stacked defense-in-depth.

14. **Scope can withstand the evolution GAN-style query red team.** 3-round runs
    (iter20, iter51) landed in the same band as 1-round runs on comparable configs
    — no visible round-over-round attrition. *Caveat: we never did a proper round-by-
    round breakdown, so this is a band-level observation rather than a rigorous claim.*

## Meta-finding — why we are pivoting

15. **The shared-prior critique.** The attacker LLM, the defender LLM, and the judge
    LLM all draw from the same training distribution. "Privacy" means the same thing
    to all three. That is why iter54 accidentally wins — its hardcoded category list
    (names/medical/financial/credentials) matches the judge's expectation. This is the
    underlying reason **no LLM-judged benchmark can separate "correctly principled" from
    "accidentally aligned."** We proved this empirically: principled policy-aware mediator
    (iter57b C 80) regressed 15 points below hardcoded-list mediator (iter54 A 95) on
    identical scope/config.

    The benchmark has exhausted its ability to guide further optimization. The signal
    from this point onward is overfit, not design.

---

## What to do next — suggestions, ranked

### 1. Wait for iter58 (running, ~90 min ETA)

**Kimi K2.6 on iter57a's config.** Pure ceiling test. If it jumps to A 94+, the
architectural ceiling is model-bound and the L3 fine-tune roadmap (DPO on Kimi K2.6
weights) becomes the most promising path. If flat (B 88–93), we are at design ceiling
on this benchmark and the pivot is unambiguously right. Either outcome is actionable —
the run costs us nothing to wait on.

### 2. Ship the pair-generation harness MVP — before any training work

The cheapest thing with real forward motion: add `POST /v1/query/pair` that runs two
mediators with different `strictness` parameters from the **same scope output**, plus a
`_hivemind_preferences` table and a terminal CLI for recording choices. ~100 LOC, no ML.

Why this first: every downstream thing (DPO, L2 profile, onboarding UX) depends on
having preference data. Build the collector, start accumulating, then figure out training.
Collecting your own preferences on your own ChatGPT data for a week is the fastest way
to discover where your privacy decisions actually live.

### 3. Write this up (short paper, ~4 pages)

The shared-prior finding is genuinely non-obvious and we have 51 iterations of clean
evidence. The iter54-vs-iter57b regression is a reproducible demonstration that LLM-
judged privacy benchmarks reward accidental alignment over principled design. This is
publishable — an empirical critique with a concrete alternative (personalization).

Likely venue: workshop paper at a privacy-ML venue, or a blog post that's actually read.
The 51-iter story is legible if compressed. The pivot proposal (preference-driven
personalization as the answer to the shared-prior problem) is a clean contribution
independent of whether the harness we build succeeds.

### 4. Validate the shared-prior critique before committing fully

Before pouring engineering into the pivot, do one low-cost sanity check: take 20 attacks
from iter54's run and have a **human** judge them. If human agreement with the LLM
judge's "SAFE/LEAK" labels matches well (>85%), the shared-prior claim weakens and the
LLM-judged benchmark is more trustworthy than we said. If human agreement is noticeably
worse (<70% on the borderline cases), the shared-prior claim is confirmed and the pivot
is clearly the right call. One evening of work.

### Honorable mention — not recommended right now

- **More prompt iterations on the 6-scenario bench.** Diminishing returns, will produce
  noise plus overfit. Every additional iter teaches us less than the last.
- **Extending `scenarios_real.json` to more sources.** Same shared-prior problem; more
  real-authored data doesn't fix it if the judge is still an LLM.
- **New scope agent superpowers (more MCP tools, more prompt sections).** The model
  already ignores simulate_multi, filesystem reads, and most of the workflow block
  when it decides to deny-first. More tools don't fix the prior; they add surface area.

### Rough ordering

- Today: let iter58 finish. Read the result, update the doc.
- This week: ship the pair-generation harness MVP. Start collecting your own
  preferences.
- Next week: write the short paper, in parallel with L2 profile plumbing.
- Month+: human-judged validation if you want a definitive answer on the shared-prior
  claim; L3 fine-tune on Kimi (K2 or K2.6 depending on iter58) once you have ≥500
  preference pairs.
