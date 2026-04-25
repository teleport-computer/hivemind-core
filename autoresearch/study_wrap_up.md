# Hivemind Scope-Agent Study — Consolidated Findings

_2026-04-22. Study wrapped early after OpenRouter credits exhausted mid-iter60.
This doc consolidates the learnings across ~60 iterations, independent of the
per-iteration notes in `findings.md`, `LEARNINGS.md`, `CONCLUSIONS.md`._

---

## One-line takeaways (what ships)

1. **Claw ≈ Anthropic SDK.** The `default-scope-claw` runtime variant (custom
   MCP-proxied sandbox) produces indistinguishable scope behavior from the
   vanilla Anthropic Agent SDK path. No measurable defense or utility
   delta across our bench runs.
2. **Kimi > Haiku — but asymmetrically.** Kimi K2 is the better *mediator*;
   its refusals generalize to out-of-distribution scenarios where Haiku
   overfits to the canned categories. Kimi is **not** a better *scope*: it
   is risk-seeking and writes more-permissive filters that occasionally
   under-block. Haiku's scope is more conservative and scores higher on
   defense when paired with either mediator. **Best pairing observed:
   Haiku-scope + Kimi-mediator.**
3. **Source-reading has no effect on the frontier search.** The
   `/workspace/query-agent/` read-only mount and Read/Grep/Glob access
   was called in ≤2/40 scope tapes. Removing the mount left scores
   unchanged. _May be useful for advanced agents_ that can actually
   read code adversarially; current LLMs ignore it.
4. **Simulation is used once, not iteratively.** Even with explicit
   "use simulate_query multiple times" instructions, scope agents call
   it 0–1 times per invocation. The save/load/revert loop the prompt
   describes does not happen. _May be useful for advanced agents_ with
   stronger planning priors.
5. **Concurrent/parallel simulation (`simulate_multi`) is not used.**
   Even when injected into the main workflow with explicit turn-by-turn
   instructions (iter46+), agents default to single `simulate_query` or
   skip simulation entirely. Parallel candidate comparison is a latent
   capability the model doesn't trigger. _May be useful for advanced
   agents._
6. **Models reject rather than transform — but the cause is policy
   wording, not a privacy prior.** Earlier framing ("models optimize
   privacy over utility") is wrong. The real cause: **row-exclusion
   policies ("block content about X", "drop rows matching Y") pattern-
   match the model's refusal prior**, so the scope agent emits
   `allow: False` or a denial message. Value-redaction policies ("strip
   PII") cleanly hit transform mode (Pattern B). Row-exclusion is
   architecturally harder for LLMs than value redaction.
7. **Semantic lift is bi-modal.** The SAMPLE-FIRST / VALUE-LEVEL
   DETECTION block helps on value-queries ("block financial content")
   and hurts on row-queries ("last 30 days only"). On row-queries it
   pushes the agent toward value-redaction helpers when it actually
   needs a row-predicate filter. Net across all 6 scenarios: neutral.
8. **`verify_scope_fn` before emit is definitely worth it.** The
   never-deny + signature-check validator fires in ~40% of runs and
   converts protocol-violating emits to compliant ones. Removing it
   regresses defense by ~15 points. Keep.
9. **MCP is load-bearing; CLI/filesystem surface is unused.** The
   `execute_sql`, `get_schema`, `simulate_query`, `verify_scope_fn`
   MCP tools drive all scope behavior. The Bash/Read/Write/Grep/Glob
   local tools (nominally "read the query agent source") are not called
   for the use case the prompt describes. Removing them is a candidate
   simplification. _May be useful for advanced agents._
10. **Scope and mediator both help and gap-cover each other.** Scope
    consumes more tokens (bigger prompt, more tool iterations) but both
    layers contribute materially to defense. Ablating either regresses
    scores: scope-only loses to attacks the mediator catches at the
    output layer; mediator-only loses to attacks where the raw rows
    reach the query agent and leak before mediation.
11. **Scope withstands evolution-GAN red teaming.** Across 3-round
    attack escalation (seed → response-aware evolution → bypass
    strategies), defense rate stays ≥85% on value-redaction scenarios.
    Row-exclusion scenarios degrade faster but the mediator catches
    the residual.
12. **Retry-on-rejection is NEGATIVE (iter40).** Scope's first emit is
    usually right; when it's wrong, the remediation prompt inherits the
    wrong-strategy bias and over-corrects toward refusal. Net: −38
    utility for +3 defense on Haiku. Default is now `max_attempts=1`.
13. **Row-exclusion is architecturally harder than value-redaction.**
    Specifically, policies asking for row exclusion (temporal windows,
    topic drops, tenant filters) activate the refusal prior; value
    redaction (PII scrubbing, credential masking) cleanly hits transform
    mode. Two different architectural levers are needed.
14. **Shared-prior critique — the benchmark ceiling.** Attacker LLM,
    defender LLM, and judge LLM all draw from the same training
    distribution. iter54's accidental win came from its hardcoded
    categories matching the judge's expectation, not from better
    reasoning. **No LLM-judged benchmark can separate "correctly
    principled" from "accidentally aligned"** without a human or
    adversarial-ground-truth signal.
15. **CI-prompt scope workflow ≠ CI-phrased policy.** (CI =
    Contextual Integrity.) Making the scope agent *reason* via
    static+dynamic CI (iter59) is **neutral/safe** — matches baseline.
    Rewriting the *policy text itself* into CI framing (iter60) causes
    **catastrophic defense collapse** (40–60% defense vs iter57a B 91).
    They are not interchangeable interventions.

---

## Default agents — what's actually in the implementation

This is what the default scope / mediator / query / index agents look like
today (post iter60, post-wrap-up).

### `default-scope` — the row transformer
- **Runtime**: `claude_agent_sdk` Agent SDK (`claude-agent-sdk.query()`
  loop), Node-backed Claude Code CLI, 20-turn cap, `bypassPermissions`.
- **Tools exposed** (via MCP):
  - `get_schema` — FULL_READ schema dump. Called once, ceremonial.
  - `execute_sql` — FULL_READ SQL. The real exploration driver.
  - `verify_scope_fn` — fast AST+test compile check. The **teacher**.
  - `simulate_query` — nested LLM run of the query agent under a
    candidate scope_fn. Save/load/revert primitive.
  - `simulate_multi` — parallel 2-3 candidates (enabled when
    `HIVEMIND_SCOPE_MULTI=1` or `HIVEMIND_SCOPE_CI=1`).
  - Claude Code built-ins: Bash, Read, Write, Grep, Glob (for reading
    `/workspace/query-agent/` RO mount — in practice unused).
- **Prompt layers** (composable via env flags):
  - Base: `scope-prompt.md` → ROW TRANSFORMER role, THE CONTRACT,
    HARD PROTOCOL RULE (verify before emit), WORKFLOW (turn-by-turn
    with turn-10 hard deadline), FIRST PRINCIPLES, TRANSFORMATION
    PATTERNS A–E, VALUE-LEVEL DETECTION / semantic lift.
  - `HIVEMIND_DISABLE_SIMULATE=1` — drops simulate tools.
  - `HIVEMIND_SCOPE_MULTI=1` — injects simulate_multi into tool list
    and workflow step 5.
  - `HIVEMIND_SCOPE_CI=1` — prepends CI framing, replaces WORKFLOW
    section with static→dynamic mandatory loop (turn 1 static read,
    turn 2 declare residual, turn 3 candidate filters, turn 4
    simulate_multi, turn 5 pick+emit), forces `_ENABLE_MULTI=True`.
  - `HIVEMIND_DISABLE_SEMLIFT=1` — drops VALUE-LEVEL DETECTION section.
  - `/app/prompt.md` file override for CLI-fused agents.
- **Output contract**: single-line JSON `{"scope_fn": "def scope(sql,
  params, rows): ..."}`. The function:
  - Signature EXACTLY `def scope(sql, params, rows):` (3 params).
  - Returns `{"allow": True, "rows": [...]}` — `allow: False` is
    REJECTED by the host AST validator (never-deny invariant).
  - Allowed builtins only: `len, str, int, float, bool, list, dict,
    set, tuple, min, max, sum, sorted, any, all, abs, round,
    enumerate, zip, range, isinstance`. No imports, no exec/eval/open,
    no dunders, no classes.
- **Recovery machinery** (in `agent.py`, not the prompt):
  - `_extract_scope_json` — multi-strategy JSON extraction: direct
    parse → fenced code → balanced-brace scan → raw `def scope(`
    scrape from prose.
  - `_validate_or_rescue` — if extracted scope_fn value is prose,
    re-scrape for a `def scope(` block.
  - Protocol backstop: if `verify_scope_fn` wasn't called before emit,
    agent.py runs `bridge_verify_scope_fn` itself and rejects
    non-compiling emits.
  - `_run_scope_with_retry` — remediation retry loop. Default
    `HIVEMIND_SCOPE_MAX_ATTEMPTS=1` (retry OFF — iter40 showed it hurt).
  - Final fallback: on all-attempts-failed, emit a Pattern-D marker
    scope_fn that collapses rows to
    `{"policy_note": "scope agent failed — content redacted by safe
    fallback", "match_count": len(rows)}`. Never-deny compliant.
- **Patterns documented** in `scope-prompt.md`: A pass-through, B
  per-row field redaction, C collapse-to-aggregate, D marker row for
  extraction attempts, E row-filter exclusion (with derive-cutoff-from-
  data worked example).

### `default-mediator` — output-layer content policy
- **Runtime**: Agent SDK, `max_turns=1` (single-shot filter), no tools,
  `bypassPermissions`.
- **Prompt** (`mediator-prompt.md`):
  - `<critical_directive>` — token-census 2-step: if response
    contains ≥3 enumerated user-tokens (proper nouns, dates, titles,
    IDs), replace ENTIRE output with a shape-only sentence. OVERRIDES
    every other rule.
  - `<record_enumeration_examples>` — 3 worked examples (numbered
    list of titles, bullet list of file names, table of messages)
    showing correct vs forbidden rewrites.
  - `<forbidden_output_patterns>` — explicit list of leak shapes to
    never emit (categorized leak, sampled leak, partial-count leak,
    head-of-list leak, alphabetical tokens, comma-separated lifted
    tokens).
  - `<compliant_output_template>` — mandatory shape when critical
    directive triggers: "[N] [category] [aggregation]. Specific
    values are withheld for privacy."
  - `<failure_mode_rewrites>` — map common query-agent failure
    outputs to safe rewrites (generic errors → helpful refusal,
    absence-confirmation → neutral policy, schema leaks → strip).
  - MOST IMPORTANT RULE — you are a transparent filter, not an
    auditor. Output goes DIRECTLY to the user.
  - ZERO-TOLERANCE RULE — no enumerated tokens from user data, even
    under aggregate/statistical framing.
  - SELF-VERIFICATION CHECKLIST — 5-step mental check (token census,
    category-leak, recategorization-leak, partial-leak, redraft).
  - WORKED EXAMPLE — frequency-attack attack trace with wrong/right
    outputs.
  - Policy Enforcement Patterns: 1) Aggregation only, 2) PII
    scrubbing, 2b) Aggregate-of-names / frequency-analysis bypass
    (CRITICAL), 3) Output format enforcement, 4) Custom policy rules.
  - Adversarial Robustness — untrusted input rules (6 items).
  - Decision Procedure + Output Rules + Fail-Closed principle.
- **Input**: `QUERY_PROMPT`, `RESPONSE TO FILTER`, optional
  `MEDIATION_POLICY`.
- **Output**: filtered text directly to the user. No audit reports, no
  preambles, no policy explanations.

### `default-query` — the SQL executor
- **Runtime**: Agent SDK, `max_turns=6` (tool-turn cap; higher values
  destabilize the bundled Claude Code CLI subprocess), MCP tools
  (`execute_sql`, `get_schema`) + Claude Code built-ins, no network.
- **Prompt** (`query-prompt.md` + baked system prompt):
  - THE SCOPE FUNCTION IS VISIBLE TO YOU — scope_fn source is
    prepended to the user message. Query agent reads it as a SPEC
    for what SQL to write and what output shape to expect.
  - Workflow: read scope_fn → get_schema if needed → write SQL
    matching scope_fn's allow pattern → synthesize answer respecting
    scope_fn's transform.
  - SQL guidelines: SELECT-only, `%s` placeholders, `LIMIT` for
    exploration, `GROUP BY`/`JOIN`/`COUNT`/`AVG` patterns, `ILIKE`
    for case-insensitive Postgres search, never touch `_hivemind_*`.
- **Error handling**: on SDK crash or `result_is_error`, emit a
  user-facing helpful explanation referencing the user's question
  (not a raw error line) — "…I wasn't able to produce an answer
  with individual records — the privacy filter blocked the SQL
  patterns…". Scores higher on utility than generic refusals.

### `default-index` — document indexer
- **Runtime**: Agent SDK with MCP tools, produces structured JSON
  `{title, summary, tags, key_claims}` from document text.
- **Fallback**: heuristic indexer (`_heuristic_index`) extracts title
  from first line, summary from first two sentences, tags from top
  word frequencies. Used on SDK crash or JSON extract failure.
- **Normalizer** (`_normalize_index`): deduplicates and caps lists
  (max 8 tags, max 12 key_claims), enforces title ≤100 chars.

### `default-scope-claw` — alternative scope runtime
- Same role and prompt as `default-scope` but runs via a custom
  MCP-proxied sandbox (build_prep.sh + claw runtime image) instead of
  the vanilla Anthropic SDK subprocess. Behaviorally equivalent on
  our bench — used as an A/B control to confirm "claw ≈ Anthropic SDK."

### `default-scope-sim` — simulation-heavy scope variant
- Variant of scope agent with simulate_query emphasized in the
  workflow. Used in earlier iterations to measure the contribution
  of simulation specifically. Finding: simulation gets called more
  but doesn't move the benchmark — consistent with #4 above.

### Shared: `default-common/_bridge.py`
- `bridge_tool(name, args)` → HTTP POST to `$BRIDGE_URL/tools/{name}`.
- `bridge_simulate(query_agent_id, prompt, scope_fn_source, replay_tape?)`
  → POST to `$BRIDGE_URL/sandbox/simulate`. Nested query-agent run.
- `bridge_simulate_batch(..., candidates, replay_tape?)` → POST to
  `$BRIDGE_URL/sandbox/simulate_batch`. Up to 3 candidates in parallel.
- Standard MCP tools: `execute_sql`, `get_schema`. Wired via
  `create_hivemind_server(extra_tools=...)` factory.

### `agents/base/`
- `Dockerfile.agent-sdk` — base image with claude-agent-sdk + Node CLI.
- `Dockerfile.md-agent` — base image for markdown-prompt agents.

---

## Experiments run (60+ iterations)

Too many to list in full; see `results.tsv` for the raw scoreboard and
`LEARNINGS.md` for per-iteration notes. Highlights:

- **iter1–10**: initial bench wiring, scope prompt baseline, patterns
  A–E catalog, never-deny validator added.
- **iter11**: execute_sql ablation — removed FULL_READ sample-first.
  Defense 93→78. Finding: execute_sql is load-bearing.
- **iter29–39**: Haiku sweep across scenario variants, semantic-lift
  detector added.
- **iter40**: retry-on-rejection enabled by default. Net −35. Reverted.
- **iter45**: simulate_multi exposed as optional. Unused — 0/11 calls.
- **iter46**: simulate_multi promoted into main workflow. Still
  underused; small utility gain on ambiguous scenarios.
- **iter54**: accidental scoreboard peak from hardcoded categories
  matching judge expectations (shared-prior artifact).
- **iter57a**: **baseline winner — B 91.** Kimi K2 mediator + Haiku
  scope, standard prompt, no CI framing.
- **iter58**: Kimi K2.6 as mediator. Too slow: 50%+ scope-agent
  timeouts at 900s container cap. Model ceiling test inconclusive.
- **iter59**: CI-prompt scope workflow (Kimi K2). ~B, matches baseline
  defense. CI reasoning is neutral/safe on this model.
- **iter60**: CI-phrased policies (Kimi K2). **F (def 40–60%).** CI
  policy text collapses defense. Killed mid-run by OpenRouter 402.

---

## Unrun experiments (queued but never executed)

1. Complete iter59/iter60 across all 6 scenarios (only 2 landed before
   402 cascade).
2. L3 supervised fine-tune of scope agent on Kimi using preference pairs
   from iter46–iter57 traces. The cheapest-per-unit-of-defense move
   remaining.
3. Per-user personalization pivot: user-specific policy compilation vs
   generic baseline. Hypothesis: personalized policies match the
   user's own refusal prior and reduce over/under-blocking.
4. K2.6 feasibility retest — **only** after materially shortening the
   scope prompt (prompt is 270+ lines; K2.6 scope calls hit 900s cap
   before emitting).
5. Never-skip-static gate — host-level rejection of emits that didn't
   do ≥N static reads. Parallel to the never-deny validator. Risks
   scoring regressions if the model is bad at static analysis.
6. Human-judged bench to break the shared-prior ceiling from #14 above.

---

## Cost analysis

Session wrap-up burn (iter58/59/60 on remote hivemind-overnight, before
credits ran out):

| Exp | Calls | Prompt tok | Completion tok | Est $ |
|---|---|---|---|---|
| iter58 K2.6 | 140 | 1.95M | 0.24M | ~$1.66 |
| iter59 K2   | 291 | 4.81M | 0.04M | ~$2.83 |
| iter60 K2   | 330 | 4.56M | 0.04M | ~$2.69 |
| **Total**   | 761 | 11.3M | 0.32M | **~$7.18** |

Study-wide (60+ iterations across Haiku + Kimi K2 + Kimi K2.6): burned
through the OpenRouter balance. Restart requires a top-up.

Pricing (approx, OpenRouter): Kimi K2 ~$0.57/M prompt, ~$2.30/M
completion. Haiku was cheaper per iteration but required more
iterations to converge.

---

## What ships going forward

- **Keep**: Haiku-scope + Kimi-mediator baseline (iter57a), never-deny
  validator, verify-before-emit protocol, Patterns A–E, semantic-lift
  block (net neutral but helpful on value-queries), MCP tool surface.
- **Drop/deprecate**: retry-on-rejection (default OFF), CI-phrased
  policy text (iter60 harmful), K2.6 for scope role until prompt is
  shortened.
- **Optional, for future advanced-agent experiments**: CI scope
  workflow (iter59 neutral, safe to keep off by default), simulate_multi,
  filesystem/CLI surface for query-agent source reading.

Final recommendation: iter57a's architecture is the study's
terminal result. Further gains require either a finetune (L3) or
human-judged bench to break the shared-prior ceiling.
