# Real-sourced benchmark inputs

The adapter (`adapter.py`) samples from human-authored privacy datasets
to produce `autoresearch/legacy_bench/scenarios_real.json` — used as a **held-out
generalization canary**, not as a training target (see
`autoresearch/CONCLUSIONS.md`).

Raw dataset files are gitignored (~100MB). Re-download with:

```bash
# PrivaCI-Bench: real HIPAA/ACLU/GDPR cases (parquet)
mkdir -p autoresearch/legacy_bench/sources/privaci_bench
# Source: https://huggingface.co/datasets/chenxubelievenai/PrivaCI-Bench
# Files: cases_HIPAA.parquet, cases_ACLU.parquet, cases_GDPR.parquet, cases_AI_ACT.parquet

# ConfAIde Tier 2a: 98 Martin & Nissenbaum vignettes with human scores
mkdir -p autoresearch/legacy_bench/sources/confaide
# Source: https://github.com/skywalker023/confAIde/tree/main/benchmark
# Files: tier_2a.txt, tier_2_labels.txt

# OPP-115: 115 real website privacy policies (currently unused, N_OPP115=0)
mkdir -p autoresearch/legacy_bench/sources/opp115
# Source: https://usableprivacy.org/data
```

After download, run:

```bash
uv run python -m autoresearch.legacy_bench.sources.adapter
```

Current sampling config (in `adapter.py`):

| Source     | N  | Why                                                      |
|------------|----|----------------------------------------------------------|
| PrivaCI    | 20 | info-type only (purpose field is domain-noisy)           |
| ConfAIde   | 15 | all vignettes with human score < −50                     |
| OPP-115    | 0  | Dropped — generic web-policy boilerplate, wrong shape    |

Total 35 scenarios with full provenance tags (`source` field points
back to the original row/line).
