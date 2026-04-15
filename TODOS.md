# TODOS

## ~~1. Build query agent eval harness~~ DONE
Built 7 scenarios (aggregation, filtering, joins, empty_tables, time_series, parameterized, schema_discovery) with 3-component scoring (SQL safety 30%, structure 40%, answer 30%). First 3 scenarios scored 100%.

## ~~2. Conditional recall explainer document~~ DONE
Written at `docs/conditional-recall.md`. Covers binary access problem, privacy-quality frontier, CLI examples, honest caveats.

## 3. Full eval coverage
**Status:** Running now
- [ ] Remaining 4 query scenarios (empty_tables, time_series, parameterized, schema_discovery)
- [ ] 8 realistic scope scenarios (chat_history, financial, health_records, social_media, etc.)
- [ ] Adversarial red team (3 rounds, 14 attack categories)

## 4. Integration test: CLI end-to-end
**What:** Test `hivemind init → scope → share → query` against a real running service.
**Status:** Blocked on having a running hivemind instance to test against.
