"""Default query agent — fully autonomous Claude Code with bridge MCP tools.

Env vars (set automatically by the sandbox):
  BRIDGE_URL, SESSION_TOKEN — bridge connection
  ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY — SDK routes LLM calls through bridge
  QUERY_PROMPT — the question to answer
  QUERY_CONTEXT — optional additional context

Outputs answer text to stdout.
"""

import asyncio
import os
import sys

from claude_agent_sdk import ClaudeAgentOptions, query
from _bridge import create_hivemind_server

QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
QUERY_CONTEXT = os.environ.get("QUERY_CONTEXT", "")

SYSTEM_PROMPT = """\
You are a query agent with access to a database via SQL tools.

You have MCP tools to access the database:
- mcp__hivemind__get_schema: Get the database schema (tables, columns, types).
- mcp__hivemind__execute_sql: Execute SQL queries against the database.

You also have local Claude Code tools (Bash, Read, Write, Grep, Glob) \
available inside your container. Note: there is NO external network access — \
tools like WebSearch and WebFetch will not work. Use MCP tools for all data access.

Workflow:
1. Call get_schema to understand the database structure.
2. Write and execute SQL queries to find relevant data.
3. Synthesize the results into a clear answer.

Rules:
- Use parameterized queries (%s placeholders) to prevent SQL injection.
- If you cannot find relevant information, say so clearly.
- Paraphrase and synthesize. Do not dump raw query results verbatim.
- Never include credentials, API keys, passwords, tokens, or secrets.
"""

server = create_hivemind_server()


async def main() -> None:
    if not QUERY_PROMPT.strip():
        print("No question provided.")
        return

    prompt = QUERY_PROMPT
    if QUERY_CONTEXT.strip():
        prompt = f"Context: {QUERY_CONTEXT}\n\nQuestion: {QUERY_PROMPT}"

    final_result = ""
    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                system_prompt=SYSTEM_PROMPT,
                mcp_servers={"hivemind": server},
                permission_mode="bypassPermissions",
            ),
        ):
            if hasattr(message, "result"):
                final_result = message.result
    except Exception as e:
        print(f"Agent SDK error: {e}", file=sys.stderr)
        print("Unable to process query due to an internal error.")
        return

    print(final_result)


if __name__ == "__main__":
    asyncio.run(main())
