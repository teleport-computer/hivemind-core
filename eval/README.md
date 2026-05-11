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
