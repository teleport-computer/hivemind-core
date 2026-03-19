# Example Agents

Ready-to-upload example agents for hivemind-core. Each directory is self-contained with a `Dockerfile` and `agent.py`.

## Default Agents

The built-in default agents (`agents/default-query/`, `default-scope/`, `default-mediator/`, `default-index/`) all use the **Claude Agent SDK** with MCP tools. They share a common bridge helper (`agents/default-common/_bridge.py`) and use `hivemind-agent-sdk-base` as their Docker base image.

```bash
# Build base image first
docker build -t hivemind-agent-sdk-base -f agents/base/Dockerfile.agent-sdk agents/base/

# Build any default agent
docker build -t hivemind-default-query agents/default-query/
```

## Upload an Example

```bash
# Pack any example into a tarball
tar czf agent.tar.gz -C agents/examples/simple-query .

# Upload to hivemind
curl -X POST http://localhost:8100/v1/agents/upload \
  -F "name=simple-query" \
  -F "archive=@agent.tar.gz"
```

## Examples

### `simple-query/` — Minimal Query Agent

The simplest possible query agent. Gets the database schema, generates a SQL query, executes it, and synthesizes an answer with one LLM call.

**Role:** query | **Tools:** execute_sql, get_schema | **LLM calls:** 2 (SQL generation + synthesize)

Good starting point for understanding the bridge contract.

### `tool-loop-query/` — Agentic Query Agent

Full agentic loop with:
- **Multi-turn tool use** — LLM decides which tools to call each turn (up to 10 turns)
- **Parallel execution** — multiple tool calls in one turn run concurrently via asyncio
- **Auto-compaction** — when context grows large, old tool results are summarized to free space
- **Structured tool calling** — LLM outputs ` ```tool ` JSON blocks, agent parses and executes

**Role:** query | **Tools:** execute_sql, get_schema | **LLM calls:** variable (up to budget)

Use this as a base for production query agents.

### `metadata-scope/` — Team-Based Scope Agent

Writes a scope function that filters SQL query results by `caller_context.team`. Only rows where the `team` column matches the caller's team pass through. If no team is specified, all results are allowed.

**Role:** scope | **Tools:** none | **LLM calls:** 0

Shows how to implement access control using scope functions.

### Tape Replay for Scope Agents

Scope agents can use **tape replay** to cheaply re-run query simulations with different scope functions. The simulate endpoint records all LLM request/response pairs as a "tape". Pass the tape back as `replay_tape` in a subsequent call with a different `scope_fn_source` — the bridge serves cached LLM responses for turns where the request hash matches (free, no budget charge). When tool results change (because the scope function changed), messages diverge naturally and live LLM calls resume.

```python
# First simulation — full cost
result = simulate(prompt, scope_fn_source="def scope(sql, params, rows): ...")
tape = result["tape"]

# Second simulation — replays the common prefix cheaply
result2 = simulate(prompt, scope_fn_source="def scope(sql, params, rows): ...", replay_tape=tape)
```

### `agent-sdk-query/` — Claude Agent SDK Query Agent

Uses the **Claude Agent SDK** with custom MCP tools instead of hand-rolled HTTP. The Agent SDK handles the agentic loop automatically — you define tools, pass a prompt, and get back a result.

Requires the `hivemind-agent-sdk-base` Docker image (build from `agents/base/Dockerfile.agent-sdk`).

```bash
docker build -t hivemind-agent-sdk-base -f agents/base/Dockerfile.agent-sdk agents/base/
docker build -t hivemind-agent-sdk-query agents/examples/agent-sdk-query/
```

**Role:** query | **Tools:** execute_sql, get_schema (via MCP) | **LLM calls:** variable (up to budget)

### `redact-mediator/` — PII Redaction Mediator

Uses an LLM to strip emails, phone numbers, API keys, and other sensitive data from query output before it reaches the caller.

**Role:** mediator | **Tools:** none | **LLM calls:** 1

Shows the mediator audit pattern.

## Agent Contract

### Environment Variables

**Enforced** (all agents receive, cannot bypass):
- `BRIDGE_URL` — HTTP endpoint for the bridge server (only allowed network exit)
- `SESSION_TOKEN` — Bearer token for bridge authentication
- `AGENT_ROLE` — Role identifier (query, scope, mediator, index)
- `BUDGET_MAX_TOKENS` — Total token budget allocated for this run
- `BUDGET_MAX_CALLS` — Total LLM call budget allocated for this run
- `OPENAI_BASE_URL` — Points to bridge's `/v1` path. Standard OpenAI SDKs auto-route through the bridge
- `OPENAI_API_KEY` — Same as `SESSION_TOKEN`. OpenAI SDKs send this as Bearer auth automatically
- `ANTHROPIC_BASE_URL` — Points to bridge root. Anthropic SDK / Claude Agent SDK auto-route through the bridge
- `ANTHROPIC_API_KEY` — Same as `SESSION_TOKEN`. Anthropic SDKs send this as `x-api-key` header

**Advisory** (default agents use these, custom agents may ignore them entirely):

| Role | Advisory Env Vars |
|------|---------------|
| **query** | `QUERY_PROMPT` |
| **scope** | `QUERY_PROMPT`, `QUERY_AGENT_ID` |
| **mediator** | `RAW_OUTPUT`, `QUERY_PROMPT` |
| **index** | `DOCUMENT_DATA`, `DOCUMENT_METADATA` |

### Bridge API

All requests require `Authorization: Bearer {SESSION_TOKEN}` or `x-api-key: {SESSION_TOKEN}`.

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Budget status (no auth needed) |
| `GET /tools` | List available tool schemas |
| `POST /tools/{name}` | Call a tool: `{"arguments": {...}}` -> `{"result": "...", "error": null}` |
| `POST /llm/chat` | LLM proxy: `{"messages": [...], "max_tokens": N}` -> `{"content": "...", "usage": {...}}` |
| `POST /v1/chat/completions` | OpenAI-compatible LLM proxy (standard OpenAI SDKs route here automatically) |
| `POST /v1/messages` | Anthropic-compatible LLM proxy (Anthropic SDK / Claude Agent SDK route here automatically) |
| `POST /v1/messages/count_tokens` | Anthropic-compatible token counting (no budget charge) |
| `POST /sandbox/simulate` | *(scope only)* Run nested query agent: `{"query_agent_id": "...", "prompt": "...", "scope_fn_source": "def scope(sql, params, rows): ...", "replay_tape": null}` |

### Output

Agents write their output to **stdout** and exit with code 0.

| Role | Output Format |
|------|--------------|
| **query** | Plain text answer |
| **scope** | `{"scope_fn": "def scope(sql, params, rows): ..."}` |
| **mediator** | Filtered/audited text |
| **index** | `{"index_text": "...", "metadata": {...}}` |
