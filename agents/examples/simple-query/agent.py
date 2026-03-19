"""Minimal query agent: get schema, run SQL, synthesize an answer.

This is the simplest possible query agent. It inspects the schema,
runs one SQL query, makes one LLM call to synthesize, and prints the answer.

Env vars (set by hivemind):
  BRIDGE_URL      — HTTP endpoint for the bridge server
  SESSION_TOKEN   — Bearer token for bridge auth
  QUERY_PROMPT    — The user's question
  QUERY_CONTEXT   — Optional additional context
"""

import json
import os

import httpx

BRIDGE = os.environ["BRIDGE_URL"]
TOKEN = os.environ["SESSION_TOKEN"]
PROMPT = os.environ.get("QUERY_PROMPT", "")
CONTEXT = os.environ.get("QUERY_CONTEXT", "")

client = httpx.Client(
    base_url=BRIDGE,
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=60,
)


def call_tool(name: str, args: dict) -> str:
    resp = client.post(f"/tools/{name}", json={"arguments": args})
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        return f"Error: {data['error']}"
    return data["result"]


def llm_call(messages: list[dict], max_tokens: int = 2048) -> str:
    resp = client.post("/llm/chat", json={"messages": messages, "max_tokens": max_tokens})
    resp.raise_for_status()
    return resp.json()["content"]


def main():
    if not PROMPT.strip():
        print("No question provided.")
        return

    # Step 1: Get the database schema
    schema_json = call_tool("get_schema", {})

    # Step 2: Ask the LLM to write a SQL query
    sql_prompt = (
        f"Database schema:\n{schema_json}\n\n"
        f"Write a SQL SELECT query to answer: {PROMPT}\n"
        "Return ONLY the SQL query, nothing else."
    )
    sql = llm_call([
        {"role": "system", "content": "You write SQL queries. Return only the SQL, no explanation."},
        {"role": "user", "content": sql_prompt},
    ], max_tokens=512)

    # Clean up SQL (strip markdown fences if present)
    sql = sql.strip().removeprefix("```sql").removeprefix("```").removesuffix("```").strip()

    # Step 3: Execute the SQL query
    results_json = call_tool("execute_sql", {"sql": sql})
    try:
        results = json.loads(results_json)
    except json.JSONDecodeError:
        results = results_json

    if not results:
        print("No relevant data found.")
        return

    # Step 4: Synthesize an answer
    user_msg = f"Based on these query results:\n\n{json.dumps(results, indent=2)}\n\nAnswer this question: {PROMPT}"
    if CONTEXT:
        user_msg = f"Context: {CONTEXT}\n\n{user_msg}"

    answer = llm_call([
        {"role": "system", "content": "Answer based on the provided data. Be concise and accurate. Do not reproduce data verbatim — paraphrase."},
        {"role": "user", "content": user_msg},
    ])

    print(answer)


if __name__ == "__main__":
    main()
