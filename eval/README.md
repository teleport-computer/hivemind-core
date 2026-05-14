# eval/

`eval/` is the active harness namespace for improving room agents. It
replaces the retired GAN-style benchmark that now lives at
`autoresearch/legacy_bench/`.

The goal is not another scalar benchmark score. The goal is to measure
latency, failure rate, leakage, and utility for the actual room data flow:

```text
room manifest
  -> adaptive scope agent
  -> query agent
  -> mediator
  -> signed output
```

## Principles

- Grade with deterministic checks first: regex/canary matching, expected
  output shape, row-level leakage probes, tool-call counts, stage latency,
  token usage, and cost.
- Report by scenario class. Do not average PII, aggregation, temporal,
  topic, trajectory, artifact, and agent-loop cases into one score.
- Keep LLM judges out of pass/fail. They may produce diagnostics, never the
  reward signal.
- Stress the scope agent's actual job: adapting to the query and query agent
  it is handed. Some scenarios should be impossible without inspecting query
  source, simulating query behavior, or auditing generated files.
- Separate fast lane from slow lane. Common aggregate analytics should finish
  without simulation; risky uploaded-agent or trajectory-sensitive tasks should
  exercise the slower superpowers.
- Treat slow-lane agent-loop competence as a measured capability, not an
  assumption. If a model/runtime cannot reliably use source inspection,
  simulation, and trajectory audit tools, the eval should show that and justify
  routing that lane through a more structured orchestrator.

## Current Harness

List deterministic scenario seeds:

```bash
uv run python -m eval list
```

Grade a saved output:

```bash
uv run python -m eval grade watch_history_top_hashtags output.md
```

or stdin:

```bash
hmctl room ask "$ROOM" "..." | uv run python -m eval grade watch_history_top_hashtags -
```

Run a scenario against a live room and store raw output, telemetry, artifacts,
and a JSON summary:

```bash
uv run python -m eval run-room watch_history_report_artifact "$ROOM" \
  --provider openrouter \
  --model z-ai/glm-5 \
  --max-tokens 1000000 \
  --max-llm-calls 60 \
  --timeout 900 \
  --fetch \
  --output-dir eval/results/live
```

The runner records:

- room id and manifest hash;
- scope/query/mediator agent ids;
- model/provider;
- per-stage latency and status;
- tool-call counts, especially scope superpower usage;
- tokens and settled cost;
- deterministic grade findings;
- report artifact filenames and fetched paths when artifacts are enabled.

## Post-Deploy Follow-Up

`.github/workflows/hermes-prod-eval.yml` runs Hermes prod canaries from the EC2
relay after successful auto-deploys. It is intentionally separate from
`.github/workflows/deploy.yml`, so live CVM deploys finish after the core deploy
and on-chain approval instead of waiting for canaries. Set repository variable
`HERMES_PROD_EVAL_ROOM` to the watch-history room id to enable automatic
follow-up evals. Optional repository variables:

- `HERMES_PROD_EVAL_MODELS` — comma-separated model ids, default `z-ai/glm-5`.
- `HERMES_PROD_EVAL_PROVIDER` — default `openrouter`.
- `HERMES_PROD_EVAL_HMCTL_PROFILE` — default `prod-eval`.
- `HERMES_PROD_EVAL_SERVICE` — default `https://hivemind.teleport.computer`.

The relay job needs tenant access to the eval room. Store that key as GitHub
secret `HERMES_PROD_EVAL_API_KEY`; the workflow writes a temporary hmctl
profile on the relay before running the scenarios.

Automatic follow-up runs always execute `watch_history_top_hashtags` as the
fast canary with `--max-tokens 250000`, `--max-llm-calls 20`, and
`--timeout 300`. In `auto` mode they only add `watch_history_report_artifact`
for Hermes agent, eval, artifact, sandbox, pipeline, dependency, or related
test changes; `HERMES_POST_DEPLOY_EVAL_MODE=full` always runs the deep
report/PDF canary and `skip` disables the automatic follow-up. The deep report
canary keeps the larger `--max-tokens 1000000`, `--max-llm-calls 60`, and
`--timeout 900` budget.

The same workflow can be manually dispatched for a single scenario against any
room, with optional artifact fetching. Follow-up evals fail their own workflow
on utility/privacy regressions, missing report artifacts, or latency over
budget. Immediately after a CVM redeploy they retry transient room-submit
502/503/504 gateway failures, but do not retry deterministic grading failures.
