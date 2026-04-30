# Pivot design sketch — personalized privacy profile via preference pairs

_2026-04-21, post iter57b. Reaction to: "on user setup they are presented
with ~10 different query pairs and they choose, and that harness is used
to fine-tune/RL the privacy/utility frontier profile."_

This is a preparation document, not an implementation. It names four
pieces we need, proposes a shape for each, and identifies the minimal
first deliverable that unblocks everything downstream.

---

## The four pieces

1. **Pair-generation harness** — one query in, two mediated outputs out,
   differing on the privacy↔utility axis. This is the atomic unit of
   preference data collection.
2. **Profile representation** — a per-user object that parameterizes
   scope_fn and/or mediator behavior. Three levels (L1/L2/L3, see below).
3. **Preference storage** — durable `(query, A, B, choice, profile_v)`
   records, keyed by user.
4. **Bootstrap query generator** — samples ~10 queries from the user's
   own data (not synthetic) that probe the decision boundary where
   reasonable users disagree.

Everything else (DPO/RL training, eval harness, UI) is downstream of
these four.

---

## Profile representation — the key design decision

Three levels, in increasing power and decreasing iteration speed:

| Level | What's parameterized              | Data shape                    | Training path             |
|-------|-----------------------------------|-------------------------------|---------------------------|
| L1    | Mediator prompt (NL preamble)     | free text                     | no training; just inject  |
| L2    | scope_fn parameters + mediator    | structured knobs (dict)       | bandit / grid search      |
| L3    | Fine-tuned model weights per user | token sequences               | DPO / RLHF on Kimi OSS    |

**Recommendation: start at L2.** Rationale:

- L1 preference data is mostly wasted if we ever move to L2 — the pairs
  were generated from prompt wording, not from structured knobs.
- L2 gives us tunable knobs *today* (a scope_fn that reads
  `redact_severity`, `allow_categories`, `block_categories` as
  parameters) while also generating preference data in a format where
  "which knob setting produced A vs B" is recoverable. That's the right
  input for eventual L3 RL on Kimi.
- L3 without L2 data is impossible (no signal to train on).

### Proposed L2 profile shape (v0)

```python
@dataclass
class Profile:
    user_id: str
    version: int                       # increments on each update
    # Scope-side knobs
    redact_severity: float             # 0.0 permissive → 1.0 strict
    block_categories: list[str]        # e.g. ["medical", "financial"]
    allow_categories: list[str]        # carve-outs
    aggregation_threshold: int         # min group size before emitting
    # Mediator-side knobs
    mediator_strictness: float         # 0.0 → 1.0
    tone: str                          # "neutral" | "first-person" | "formal"
    # Provenance
    bootstrap_pairs: list[str]         # preference row ids that produced this
    created_at: datetime
    updated_at: datetime
```

The scope_fn template becomes:

```python
def scope(sql, params, rows, profile):  # profile is new arg
    # profile.redact_severity, profile.block_categories read here
    ...
```

Which is a breaking change to the scope_fn signature. Two migration
options:

- **A: thread profile through** — change `apply_scope_fn` to pass a
  profile dict. All current scope_fns keep working because they don't
  reference it; new scope_fns opt in.
- **B: closure** — `compile_scope_fn(source, profile)` binds profile
  into the closure. Source code for scope_fn stays 3-arg. More opaque.

Option A is cleaner for debugging and RL (the profile is visible in the
trace).

---

## Pair-generation harness

The pipeline currently is `scope → query → mediator → output`. The
harness mode runs two mediators in parallel with different strictness,
returning a `QueryResponsePair` instead of a `QueryResponse`.

### Contract

```python
# new endpoint
POST /v1/query/pair
{
  "query": "...",
  "query_agent_id": "default-query",
  "scope_agent_id": "default-scope",
  "mediator_agent_id": "default-mediator",
  "profile_id": "user_42",           # optional; defaults to baseline
  "delta_axis": "strictness"         # which axis to vary A vs B on
}
→
{
  "query": "...",
  "A": {"mediated": "...", "profile_snapshot": {...}, "trace_id": "..."},
  "B": {"mediated": "...", "profile_snapshot": {...}, "trace_id": "..."},
  "pair_id": "pair_abc123"
}

# followed by
POST /v1/preferences
{"pair_id": "pair_abc123", "choice": "A" | "B" | "neither"}
```

Cheapest implementation: the same scope run feeds both mediators.
Mediator A gets `strictness=0.3`, Mediator B gets `strictness=0.8`.
Same policy, same RAW_OUTPUT, different dial.

### Delta axes (worth varying independently)

- `strictness` — amount of redaction
- `granularity` — individual vs aggregate
- `tone` — clinical vs conversational
- `explanation` — "I can't show X" vs silent elision

The bootstrap harness should vary one axis per pair so each preference
signal attributes cleanly.

---

## Preference storage

```sql
CREATE TABLE _hivemind_preferences (
  id             SERIAL PRIMARY KEY,
  user_id        TEXT NOT NULL,
  query          TEXT NOT NULL,
  variant_a_text TEXT NOT NULL,
  variant_a_profile JSONB NOT NULL,
  variant_b_text TEXT NOT NULL,
  variant_b_profile JSONB NOT NULL,
  delta_axis     TEXT NOT NULL,
  choice         TEXT,       -- 'A' | 'B' | 'neither' | NULL while pending
  chosen_at      TIMESTAMPTZ,
  created_at     TIMESTAMPTZ DEFAULT now(),
  trace_a_id     TEXT,
  trace_b_id     TEXT
);

CREATE TABLE _hivemind_profiles (
  user_id        TEXT PRIMARY KEY,
  version        INT NOT NULL DEFAULT 1,
  profile_json   JSONB NOT NULL,
  bootstrap_completed_at TIMESTAMPTZ,
  updated_at     TIMESTAMPTZ DEFAULT now()
);
```

---

## Bootstrap query generator

Critical constraint: queries must be **drawn from the user's own
data**, so the preferences are real not hypothetical. Synthetic queries
("what if someone asked about your medical history") don't capture
their actual decision space.

### Sketch

```python
def bootstrap_queries(db, user_id: str, n: int = 10) -> list[str]:
    """
    Sample n queries that probe the privacy decision boundary using
    patterns grounded in the user's actual content.

    Strategy:
    1. Inspect user's data schema + a stratified sample of rows
    2. Identify N content clusters (e.g. by topic TF-IDF)
    3. For each cluster, instantiate a query template against real
       content. Example: if a cluster is about "work conflicts",
       generate "summarize what frustrated me at work this year"
       using that cluster's top terms.
    4. Prefer clusters where the privacy decision is contested —
       e.g. clusters with mixed-sensitivity signals.
    """
    ...
```

The generator itself does NOT use an LLM for the query text — it uses
templates over real terms. (LLM-generated queries would reintroduce the
shared-prior problem.)

---

## Held-out eval (what happens to scenarios_real.json)

The real-sourced benchmark we built (35 scenarios from PrivaCI + ConfAIde)
becomes a **generalization monitor**, not a training target:

- After each user's profile is tuned on their own preference pairs,
  run the held-out benchmark on a generic persona.
- If the tuned profile still respects HIPAA-ish / ConfAIde-ish norms on
  a synthetic general user, that's evidence the personalization didn't
  overfit into "release everything."
- This flips the role from "optimize against" to "canary."

---

## Minimum first deliverable (if user approves direction)

Before any RL, the smallest thing worth shipping is the harness itself:

1. **New endpoint `POST /v1/query/pair`** — runs scope once, spawns two
   mediator sandboxes with different strictness, returns both outputs.
2. **Profile dataclass + `_hivemind_profiles` table** — L2 shape above,
   default-baseline profile created at user first-contact.
3. **Preference table + `POST /v1/preferences`** — just persistence, no
   learning yet.
4. **CLI: `eval/bootstrap.py <user_id>`** — runs 10 bootstrap queries
   against the running server, prints pairs, accepts terminal input for
   choice, stores preferences.

This is ~400 LOC. No RL, no fine-tune, no model changes. Once it's
shipped we have a preference-collection flywheel turning; training is a
separate downstream decision.

---

## Open questions for user

1. **Confirm L2 direction.** L1 is faster to ship but L1 data doesn't
   transfer. Commit to L2?
2. **What axes to expose.** Start with just `strictness`, or all four
   (strictness, granularity, tone, explanation) on day 1?
3. **User-first or feature-first.** Build the full L2 profile type and
   wire it end-to-end, or ship the minimum viable pair endpoint first
   (hardcoded two profiles: `strict` / `lenient`) and grow?
4. **Where does bootstrap run — onboarding-only, or continuous?** If
   continuous, production queries become silent A/B, which needs UX
   thinking (user consent to be sampled).
