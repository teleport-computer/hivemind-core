# bench/ — retired

The GAN-style adversarial benchmark is retired as of 2026-04-23.

## Why

See `autoresearch/LEARNINGS.md` (commit `7c6bb19`), finding #15: the LLM judge, attacker, and defender all draw from the same training distribution, so the benchmark rewards *accidental alignment* with the judge's prior rather than *principled privacy design*. Iter54 (hardcoded category list) scoring A 95 vs iter57b (policy-aware mediator) scoring C 80 on identical scope config is the reproducible demonstration: more principled design regressed 15 points because the judge happened to share the hardcoded list's taxonomy.

The benchmark exhausted its signal after 51 iterations. Further runs produce noise or overfit, not design guidance.

## Pivot

LEARNINGS next-step #2 is the pair-generation harness: `POST /v1/query/pair` runs two mediators with different `strictness` from the same scope output; a `_hivemind_preferences` table records the user's A/B choice. Preference data is what subsequent personalization work (L2 profile, DPO fine-tunes) needs.

## Code here still works

Nothing in `bench/` is broken — the GAN loop, scenario definitions, red-team evolver, and LLM judge all run. If you need to re-validate a model change against adversarial queries later (e.g. "does the new scope prompt still defend 100% on PII after a refactor?"), run:

```
python -m bench.cli run --url <server> --scenario pii_redaction --rounds 1
```

Just treat results as a sanity check, not a leaderboard.

## Canonical results

| Config                             | Grade | Def | Util |
|------------------------------------|-------|-----|------|
| iter54 kimi mediator-only          | A 95  | 100 | 84   |
| iter57a kimi full + policy-aware   | B 91  | 94  | 84   |
| iter48 kimi same-arch              | B 88  | 94  | 74   |
| iter49 haiku no-mediator           | C 83  | 84  | 81   |
| iter47 haiku baseline (current)    | C 79  | 81  | 74   |
| iter53 kimi scope-only             | C 76  | 81  | 64   |

Sanity smoke (2026-04-23, haiku, pii_redaction only): B 87 — consistent with iter47.
