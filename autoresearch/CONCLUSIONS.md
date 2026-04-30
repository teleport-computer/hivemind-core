# Hivemind — what we learned from 51 iterations

_2026-04-16 → 2026-04-21. `baseline-rewrite` (F 0) → `iter57a-kimi-full-policy-aware` (B 91)._

Closing document. What follows is the synthesis, not the log. The
running log is in `findings.md`; the raw numbers are in
`results.tsv` (51 rows).

---

## TL;DR

After 51 iterations (prompt surgery, architectural ablations, three
model sweeps, runtime swaps, and three GAN-harness rebuilds):

- The full-stack defense peaked at **B 91** (iter57a, Kimi K2 with
  policy-aware mediator). The highest single score, **A 95** (iter54,
  Kimi mediator-only with hardcoded PII categories), is almost
  certainly benchmark overfit — when we replaced the hardcoded list
  with principled policy-awareness, scores regressed 15 points.
- The architecture (scope_fn → query agent → mediator, with an
  AST-validated row-transformer scope) is sound. The top contributor
  is `verify_scope_fn` with the never-deny rule — it teaches the model
  mid-loop. Everything else is +5/−5 noise in aggregate.
- Prompt engineering has hit diminishing returns. We are within ~5
  points of the ceiling of what instruction surgery alone can reach.
- The 6-scenario GAN benchmark is no longer a useful optimization
  target. Principled changes regress it; accidental prior-alignment
  wins it. Any further gains on this bench would be overfit.

The next phase is not "more iterations." It is a pivot from
global-benchmark to personalized-preference. Design sketch:
`autoresearch/pivot_design.md`.

---

## Timeline at a glance

| Phase | Iters | Theme | Where we landed |
|---|---|---|---|
| Infrastructure | 1–16 | Get any config running | C 78 baseline |
| Prompt scaffold | 17–19 | never-deny + row-transformer + save/load NPC | B 93 (single-scenario) |
| Full 6-scenario GAN | 20–22 | Scale up; policy plumbing | B 85 overall |
| Ablation sweep | 23–33 | Turn off each superpower, measure | C 76 – C 84 band |
| OSS model sweep | 34 + 42 | DeepSeek, Kimi, Qwen, Llama via OpenRouter | Kimi K2 wins consistently |
| Decomposition study | 47–54 | Scope-only vs mediator-only vs full | **A 95** (iter54, mediator-only Kimi) |
| Principled mediator | 57a/b | Policy-aware prompt | B 91 with scope; C 80 without |

---

## The seven learnings

### 1. Scope agent has two execution modes; only one works

Trace-level reading of ~40 scope runs: the model enters either
**transform-first exploration** (12–17 LLM calls, iterative validator
use, success) or **deny-first chain-of-thought** (3 LLM calls, skip
validator, ship a wrong-signature or `{"allow": False}` scope_fn,
fail). Which mode fires is determined by the policy phrasing's
activation of the model's refusal prior, *not* by the prompt's
instructions. Topic-filter and temporal-window policies ("block
content about X") strongly activate the deny prior; value-redaction
policies ("redact names in output") activate the transform prior.

This is the **architectural ceiling on row-exclusion**: Haiku/Kimi
consistently underperform on temporal_scoping and topic_filtering
(often 60–80 defense) regardless of prompt changes, because those
policies push the model into mode 2.

### 2. `verify_scope_fn` is the load-bearing teacher

The AST validator with deny-rejection rule is the single most
impactful component we added. In success-mode traces it is called
3–5 times per scope invocation, each error message correcting a
specific contract violation (signature, return shape, deny-literal,
etc.). Ablating it was never attempted because we knew it would
collapse grades to D/F — the extractor alone can't rescue
structurally-wrong scope_fns.

Every other superpower (`simulate_query`, filesystem reads,
`execute_sql`) is context without correction signal, and their
per-iteration values are much smaller (+4 / −2 / ±0). Lesson: **tools
without a teacher are decoration**.

### 3. Superpowers are bimodal, not additive

Turning off individual features rarely moves the aggregate score by
more than a few points — but the per-scenario effects are large and
they cancel in the average:

| Ablation | Overall Δ | Best per-scenario Δ | Worst per-scenario Δ |
|---|---|---|---|
| no semantic-lift (sample-first) | +2 | +28 topic_filtering | −26 pii_redaction |
| no policy plumbing | −6 | +2 aggregation | −26 temporal_scoping |
| no simulate_query | −2 | +20 temporal_scoping | −20 aggregation |
| no never-deny | +2 | +12 temporal | −12 pii |

Reading the aggregate misses the story. The same feature that unlocks
value-redaction (+20 PII on sem-lift) sabotages row-exclusion (−28
topic). The lesson: **don't tune against the overall grade; tune
against per-policy profiles**. This also motivates the pivot —
no single global tuning works for every scenario.

### 4. Scope and mediator are non-independent; they gap-cover each other

Single-component ablations looked suspiciously close to the full
stack:

| Config (Haiku) | Defense | Utility | Overall |
|---|---|---|---|
| Full stack (iter47) | 81 | 74 | C 79 |
| Scope-only, no mediator (iter49) | 84 | 81 | C 83 |
| Mediator-only, no scope (iter50) | 77 | 84 | C 79 |

Each single layer scores *within the same band* as the full stack.
That doesn't mean the layers are redundant — it means each is
covering a different subset of attacks. Removing one layer exposes
the attacks the other wasn't catching; the remaining layer partially
compensates. The architecture works, but not as cleanly-stacked
defense-in-depth.

This has architectural implications: optimizing one layer in
isolation (e.g. a smarter scope_fn) doesn't necessarily add to the
other layer's contribution.

### 5. Principled improvements regress the benchmark

The cleanest example is iter57a/b. We added `MEDIATION_POLICY`
plumbing so the mediator reads the scenario's POLICY (instead of a
hardcoded "strip names/medical/financial/credentials" list).

| Config | Def | Util | Overall |
|---|---|---|---|
| iter48: Kimi full, **baked-in** mediator | 94 | 74 | B 88 |
| iter54: Kimi mediator-only, **baked-in** | 100 | 84 | **A 95** |
| iter57a: Kimi full, **policy-aware** mediator | 94 | 84 | B 91 |
| iter57b: Kimi mediator-only, **policy-aware** | 87 | 64 | C 80 |

Policy-awareness helped the full-stack config (+3 overall) but
crushed mediator-only (−15). The reason: iter54's hardcoded list is
an accidental match for the 6 benchmark scenarios. Principled
policy-awareness stops over-stripping for categories the scenario
policy didn't mention — which the judge reads as *both* a leak and a
utility drop.

iter54 is a local maximum on this benchmark, but not a design we'd
ship. And any principled change will score below it. That means the
benchmark cannot distinguish "this is closer to the right design"
from "this regressed." It has exhausted its ability to guide us.

### 6. Model choice dominates; runtime is noise

Across four models (Haiku 4.5, Kimi K2, DeepSeek V3.1, Qwen) and two
runtimes (claude-agent-sdk vs claw-code):

- Same architecture, different runtime: within ±3 defense, ±40%
  wall time. Not worth swapping.
- Same runtime, different model: Kimi K2 consistently +6 to +13
  overall vs Haiku. DeepSeek = Haiku on score, but 10× slower via
  OpenRouter (5.7h vs 34min per bench).

Kimi K2 is the current best iteration model. Retaining OSS
compatibility matters because L3 of the pivot (per-user fine-tune)
requires weights we can train on.

### 7. Retry-on-rejection is negative

iter40 tested `HIVEMIND_SCOPE_MAX_ATTEMPTS` > 1 (re-invoke scope when
the extractor rejects its emit). Regression. Scope's first emit is
usually right — if it was wrong, the wrongness came from turn-1
strategy bias (see Learning #1), and a retry inherits that bias
rather than fixing it. Shipping with `MAX_ATTEMPTS=1` (commit d54841e).

---

## Architecture insights

### What works

- **AST-validated scope_fn with never-deny rule.** The validator's
  error messages do real teaching mid-loop. Row-transformer framing
  (returns `{"allow": True, "rows": [...]}`, never `{"allow": False}`)
  resolves the deny-first failure mode for value-redaction policies.
- **POLICY plumbing to scope.** Since iter22, the scenario policy is
  in the scope agent's user prompt. This is worth +8 overall on
  Haiku, +26 on temporal_scoping specifically. Right abstraction.
- **Policy-aware mediator when paired with scope.** iter57a's +3
  over iter48 is small but clean. When scope filters rows by policy,
  the mediator reading the same policy can be surgical on residue
  instead of broad-spectrum stripping.
- **Fail-closed extractor.** Scrapes `def scope(` blocks out of
  markdown/prose preambles; rejects anything without the correct
  signature. Catches the structural chain-of-thought-bypass failure
  mode without needing a re-invoke.
- **Agent SDK + MCP tools exclusively.** Scope never uses Claude
  Code's Bash/Read/Grep/Glob for verify/sql/simulate. MCP is the
  preferred surface at ratio 100:0. CLI builtins are dead weight for
  this agent.

### What doesn't

- **Source-reading of `/workspace/query-agent/`.** Observed in 2 of 40
  tapes. Simulate subsumes its information content. Keep the mount
  (zero cost) but don't design for it.
- **simulate_query as 1-shot sanity check.** Used once per run, post-
  verify, read and shipped. The "save/load NPC zero-cost revert"
  metaphor is not being exercised. simulate_multi (iter45) was
  supposed to fix this by running 2–3 strategies in parallel; the
  agent never called it (0/11 runs even after promoting it into the
  canonical workflow in iter46). Dead feature — scope defaults to
  single simulate_query regardless of prompt surgery.
- **Policy-type classifier (iter44).** A prompt prefix classifying
  policies into 4 types (VALUE/ROW/AGGREGATE/INJECTION) and routing
  strategy accordingly. Reverted pre-launch — the 4-type taxonomy
  mapped 1:1 to the 6 benchmark scenarios. Overfit by construction.
- **Filesystem mount of agent source.** iter15 (mount off A/B): same
  score band as mount-on. Keep it disabled as a cost optimization.

### The three-layer gap-covering property

Scope, query agent, and mediator aren't cleanly stacked defense-in-
depth. Each covers a different fraction of attacks. When one is
removed, the remaining two partially compensate — which is why
single-layer ablations sit in the same score band as the full stack
(see Learning #4). But it means:

- Optimizing one layer in isolation has diminishing marginal return.
- Layer gaps are not visible from the aggregate grade.
- Trace-reading (per-attack analysis) is required to attribute
  credit correctly.

### The benchmark ceiling

Current frontier, with the sharper datapoints:

- **B 91** (iter57a): top principled full-stack config. Likely close
  to the true architectural ceiling for the 6 canonical scenarios.
- **A 95** (iter54): top overall, but driven by accidental prior-
  alignment between the hardcoded mediator list and the benchmark's
  category choices.
- Further prompt iteration on the current benchmark will produce
  ±5-point movements around these numbers. We are in the noise band.

### What transferred across models

Architecture transfers: same scope_fn contract, same MCP tools, same
never-deny rule work on Haiku 4.5, Kimi K2, DeepSeek V3.1. Per-scenario
profiles differ dramatically — DeepSeek is −38 on PII vs Haiku but +14
on content_sanitization. Model personality shapes which scenarios are
hardest, not whether the architecture is viable.

This matters for the pivot: the architecture is robust enough to
port to whatever model we eventually fine-tune (Kimi K2 preferred).

---

## Benchmark critique

The 6-scenario GAN benchmark (`autoresearch/legacy_bench/scenarios.py`) served its
purpose and is now misleading us:

- Scenarios were hand-picked from a small category list (PII,
  aggregation, topic, temporal, sanitization, injection). The judge
  LLM and the attacker LLM share prior knowledge of what "privacy"
  looks like in these categories — same training distribution as the
  defender LLM.
- Shared-prior → accidental alignment between a hardcoded mediator
  and the judge's expectations gets rewarded (iter54 A 95).
- Any defense choice that narrows to a principled policy instead of
  broad-spectrum PII stripping loses coverage on scenarios the
  principled policy doesn't mention, even when that's the correct
  behavior.

We built `autoresearch/legacy_bench/sources/adapter.py` to sample from real-authored
policy data (PrivaCI-Bench HIPAA/ACLU, ConfAIde Tier 2a with human
scores) as a harder generalization test. 35 scenarios in
`autoresearch/legacy_bench/scenarios_real.json`. This is useful as a **held-out
generalization canary**, not as a new training target. The underlying
shared-prior problem doesn't disappear just because the scenarios are
human-sourced — the judge and defender are still the same LLM.

---

## What this means for the next phase

Three points to carry forward:

1. **Stop optimizing the 6-scenario GAN.** Any further iteration on
   it will be noise plus overfit. Keep it running as a regression
   canary only.
2. **Personalization is the right answer to the shared-prior problem.**
   A per-user preference signal ("do I consider this leak?") is out-
   of-distribution from the LLM's training data by construction. It's
   the one signal that can't be hallucinated from priors.
3. **L2 profile plumbing is the minimum viable pivot.** Scope_fn and
   mediator already have the infrastructure to take parameters
   (POLICY plumbing is the precedent). Adding a `profile` argument
   that carries `redact_severity`, `block_categories`, etc. is a
   small extension, not a rewrite.

Detailed design: `autoresearch/pivot_design.md`. Open decisions
(L1 vs L2 vs L3, single axis vs multi, onboarding-only vs continuous
sampling) are captured there.

---

## Artifacts from this phase

Code:
- `agents/default-scope/agent.py` — row-transformer, never-deny,
  gated semantic-lift (iter55 Option A), POLICY plumbing
- `agents/default-mediator/agent.py` — policy-aware prompt with
  baked-in fallback (iter57)
- `hivemind/pipeline.py` — MEDIATION_POLICY plumbing in sync path
- `hivemind/scope.py` — AST validator + extractor rescue path
- `autoresearch/legacy_bench/scenarios.py` — 6-scenario GAN (regression canary from here)
- `autoresearch/legacy_bench/sources/adapter.py` — real-sourced benchmark (35 scenarios,
  held-out canary)
- `autoresearch/parallel_ablations.sh` — orchestrator for disjoint-
  port experiments

Infrastructure:
- t3.large with Docker + Postgres 16, 912 conversations / 17365 msgs
  loaded, trace capture via `HIVEMIND_TRACE_DIR`
- `watch/dashboard.py` live UI (Caddy + LE TLS)
- Bridge tape recorder for request/response replay

Writing:
- `autoresearch/findings.md` — running log, trace-level analysis
- `autoresearch/ablation_analysis.md` — iter 17-33 ablation sweep
- `autoresearch/pivot_design.md` — next-phase sketch
- `autoresearch/results.tsv` — 51 rows of grade + commentary
