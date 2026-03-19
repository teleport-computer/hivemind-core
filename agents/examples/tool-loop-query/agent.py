"""Query agent with agentic tool loop, parallel execution, and auto-compaction.

This agent implements the full agentic pattern:
  - Multi-turn tool loop (LLM decides which tools to call)
  - Parallel tool execution (multiple calls in one turn run concurrently)
  - Auto-compaction (old tool results are summarized when context grows large)
  - Structured tool calling via JSON blocks in LLM output

Env vars (set by hivemind):
  BRIDGE_URL      — HTTP endpoint for the bridge server
  SESSION_TOKEN   — Bearer token for bridge auth
  QUERY_PROMPT    — The user's question
  QUERY_CONTEXT   — Optional additional context
"""

import asyncio
import json
import os

import httpx

BRIDGE = os.environ["BRIDGE_URL"]
TOKEN = os.environ["SESSION_TOKEN"]
PROMPT = os.environ.get("QUERY_PROMPT", "")
CONTEXT = os.environ.get("QUERY_CONTEXT", "")

MAX_TURNS = 10
COMPACTION_CHAR_THRESHOLD = 80_000  # ~20k tokens
COMPACTION_KEEP_RECENT = 4  # keep last N assistant+tool exchanges

SYSTEM_PROMPT = """\
You are a query agent with SQL access to a database. Answer the question \
using the provided tools.

Available tools:
- get_schema(): Get the database schema (tables, columns, types). Call this first.
- execute_sql(sql, params=[]): Execute a SQL query. Use %s for parameter placeholders. \
Returns JSON rows for SELECT queries.

To call tools, output one or more JSON blocks like this:
```tool
{"name": "get_schema", "arguments": {}}
```

```tool
{"name": "execute_sql", "arguments": {"sql": "SELECT * FROM users WHERE team = %s", "params": ["alpha"]}}
```

You can call multiple tools in one response — they run in parallel.

When you have enough information to answer, just write your answer as plain text \
(no tool blocks). Be concise and accurate. Paraphrase — do not reproduce data verbatim. \
If nothing relevant is found, say so honestly.\
"""


def make_client() -> httpx.Client:
    return httpx.Client(
        base_url=BRIDGE,
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=60,
    )


# ── Bridge calls ──

def call_tool(client: httpx.Client, name: str, args: dict) -> str:
    resp = client.post(f"/tools/{name}", json={"arguments": args})
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        return f"Error: {data['error']}"
    return data["result"]


def llm_call(client: httpx.Client, messages: list[dict], max_tokens: int = 4096) -> str:
    resp = client.post("/llm/chat", json={"messages": messages, "max_tokens": max_tokens})
    if resp.status_code == 429:
        return "(Budget exhausted — cannot make more LLM calls.)"
    resp.raise_for_status()
    return resp.json()["content"]


# ── Tool call parsing ──

def parse_tool_calls(text: str) -> tuple[list[dict], str]:
    """Extract ```tool blocks from LLM response.

    Returns (tool_calls, remaining_text).
    Each tool_call is {"name": str, "arguments": dict}.
    """
    calls = []
    remaining_lines = []
    in_block = False
    block_lines = []

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped == "```tool":
            in_block = True
            block_lines = []
        elif in_block and stripped == "```":
            in_block = False
            raw = "\n".join(block_lines).strip()
            try:
                parsed = json.loads(raw)
                if "name" in parsed:
                    calls.append(parsed)
            except json.JSONDecodeError:
                remaining_lines.append(f"(Failed to parse tool call: {raw[:100]})")
        elif in_block:
            block_lines.append(line)
        else:
            remaining_lines.append(line)

    return calls, "\n".join(remaining_lines).strip()


# ── Parallel tool execution ──

async def execute_tools_parallel(client: httpx.Client, calls: list[dict]) -> list[dict]:
    """Execute multiple tool calls concurrently. Returns list of results."""

    async def run_one(call: dict) -> dict:
        name = call.get("name", "")
        args = call.get("arguments", {})
        result = await asyncio.to_thread(call_tool, client, name, args)
        return {"name": name, "arguments": args, "result": result}

    return await asyncio.gather(*(run_one(c) for c in calls))


# ── Context compaction ──

def estimate_chars(messages: list[dict]) -> int:
    return sum(len(m.get("content", "")) for m in messages)


def compact_context(messages: list[dict]) -> list[dict]:
    """Summarize old tool exchanges to free context space.

    Keeps the system message, first user message, and the last
    COMPACTION_KEEP_RECENT assistant+user exchanges. Older exchanges
    are replaced with a bullet-point summary.
    """
    # Find turn boundaries (assistant messages)
    turn_starts = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]

    if len(turn_starts) <= COMPACTION_KEEP_RECENT:
        return messages

    # Split: system + first user | old turns | recent turns
    # messages[0] = system, messages[1] = user prompt
    cutoff = turn_starts[-COMPACTION_KEEP_RECENT]
    old_section = messages[2:cutoff]
    recent_section = messages[cutoff:]

    summaries = []
    for m in old_section:
        content = m.get("content", "")
        role = m.get("role", "")
        if role == "assistant":
            # Summarize tool calls made
            preview = content[:150] + "..." if len(content) > 150 else content
            summaries.append(f"- Assistant: {preview}")
        elif role == "user" and "Tool results:" in content:
            # Summarize tool results
            lines = content.split("\n")[:5]
            for line in lines:
                if line.startswith("["):
                    summaries.append(f"- {line[:150]}")

    if not summaries:
        return messages

    summary_text = (
        "[Earlier tool interactions compacted to save context]\n"
        + "\n".join(summaries[:15])
    )

    return [
        messages[0],  # system
        messages[1],  # original user prompt
        {"role": "assistant", "content": summary_text},
        {"role": "user", "content": "(continuing from compacted context)"},
    ] + recent_section


# ── Main loop ──

async def agent_loop():
    if not PROMPT.strip():
        print("No question provided.")
        return

    client = make_client()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    user_msg = PROMPT
    if CONTEXT:
        user_msg = f"Context: {CONTEXT}\n\nQuestion: {PROMPT}"
    messages.append({"role": "user", "content": user_msg})

    for turn in range(MAX_TURNS):
        # Auto-compact if context is getting large
        if estimate_chars(messages) > COMPACTION_CHAR_THRESHOLD:
            messages = compact_context(messages)

        # Call LLM
        response = llm_call(client, messages)

        # Parse tool calls
        tool_calls, remaining_text = parse_tool_calls(response)

        if not tool_calls:
            # No tool calls — this is the final answer
            print(remaining_text or response)
            return

        # Execute tool calls in parallel
        messages.append({"role": "assistant", "content": response})
        results = await execute_tools_parallel(client, tool_calls)

        # Format results and feed back
        result_lines = []
        for r in results:
            result_lines.append(f"[{r['name']}({json.dumps(r['arguments'])})]")
            result_lines.append(r["result"])
            result_lines.append("")

        messages.append({"role": "user", "content": "Tool results:\n" + "\n".join(result_lines)})

    # Exhausted turns — print whatever we have
    print(remaining_text or "(Reached maximum turns without a final answer.)")


def main():
    asyncio.run(agent_loop())


if __name__ == "__main__":
    main()
