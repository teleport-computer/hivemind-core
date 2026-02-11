#!/usr/bin/env python3
"""Example scoping agent: proximity-based scope with query agent source inspection.

Receives:
- PROMPT: the query question
- QUERIER_ID: who is asking (may be empty)
- QUERY_AGENT_ID: which query agent will process the scoped data ("default" or an ID)
- QUERY_AGENT_IMAGE: Docker image of the query agent
- Access to tools: search_index, read_record, list_index, list_by_user, list_users
  (FULL DB — no scope restrictions)
- Access to agent file tools: list_query_agent_files, read_query_agent_file
  (inspect the query agent's source code)
- Access to LLM: /llm/chat

Outputs JSON to stdout:
{"record_ids": ["abc123", "def456", ...]}

This whitelist becomes the query agent's entire visible universe.

Strategy (proximity scoping + source inspection):
1. Find the querier's own records via list_by_user(QUERIER_ID)
2. Extract their tag/topic profile
3. Search for records with overlapping tags from other users
4. Inspect the query agent's source code to assess trustworthiness
5. Output the union of the querier's records + topically nearby records
"""
import json
import os
import urllib.request

BRIDGE = os.environ["BRIDGE_URL"]
TOKEN = os.environ["SESSION_TOKEN"]
QUESTION = os.environ.get("PROMPT", "")
QUERIER_ID = os.environ.get("QUERIER_ID", "")
QUERY_AGENT_ID = os.environ.get("QUERY_AGENT_ID", "default")
QUERY_AGENT_IMAGE = os.environ.get("QUERY_AGENT_IMAGE", "")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{BRIDGE}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


record_ids = set()

# Step 1: Find querier's own records using list_by_user
if QUERIER_ID:
    try:
        querier_result = call(
            "POST",
            "/tools/list_by_user",
            {"arguments": {"user_id": QUERIER_ID, "limit": 50}},
        )
        results = json.loads(querier_result.get("result", "[]"))

        # Collect querier's tags as their "profile"
        querier_tags = set()
        for item in results:
            record_ids.add(item["record_id"])
            tags_str = item.get("tags", "")
            querier_tags.update(t.strip() for t in tags_str.split(",") if t.strip())
    except Exception:
        querier_tags = set()
else:
    querier_tags = set()

# Step 2: Search for topically nearby records using querier's tags + question
search_terms = " ".join(list(querier_tags)[:5])
if not search_terms:
    search_terms = QUESTION

try:
    nearby = call(
        "POST",
        "/tools/search_index",
        {"arguments": {"query": search_terms, "limit": 50}},
    )
    for item in json.loads(nearby.get("result", "[]")):
        record_ids.add(item["record_id"])
except Exception:
    pass

# Step 3: Also search using the question directly for relevance
try:
    question_results = call(
        "POST",
        "/tools/search_index",
        {"arguments": {"query": QUESTION, "limit": 20}},
    )
    for item in json.loads(question_results.get("result", "[]")):
        record_ids.add(item["record_id"])
except Exception:
    pass

# Step 4: Inspect query agent source code to assess trustworthiness.
# The scoping agent can read the actual source files from the query agent's
# Docker image to decide what data the agent should be allowed to see.
if QUERY_AGENT_ID != "default":
    try:
        # List the query agent's source files
        file_list = call("POST", "/tools/list_query_agent_files", {"arguments": {}})
        files_data = json.loads(file_list.get("result", "{}"))
        agent_files = files_data.get("files", [])

        if agent_files:
            # Read the main source file (or all small files)
            source_snippets = []
            for f in agent_files[:5]:  # read up to 5 files
                content = call(
                    "POST",
                    "/tools/read_query_agent_file",
                    {"arguments": {"file_path": f["path"]}},
                )
                source_snippets.append(
                    f"=== {f['path']} ===\n{content.get('result', '')[:2000]}"
                )

            # Use LLM to assess whether the code is safe
            assessment = call("POST", "/llm/chat", {
                "messages": [{
                    "role": "user",
                    "content": (
                        "Analyze this agent's source code. Does it:\n"
                        "1. Print raw record text verbatim to stdout?\n"
                        "2. Attempt to exfiltrate data (encoding, steganography)?\n"
                        "3. Do anything beyond search + summarize?\n\n"
                        + "\n\n".join(source_snippets) + "\n\n"
                        "Answer: SAFE, SUSPICIOUS, or DANGEROUS with one line explanation."
                    ),
                }],
                "max_tokens": 100,
            })
            verdict = assessment.get("content", "").upper()
            if "DANGEROUS" in verdict:
                record_ids = set()  # deny all access
            elif "SUSPICIOUS" in verdict:
                record_ids = set(list(record_ids)[:5])  # minimal access
        else:
            # No source files extracted — compiled binary, be conservative
            record_ids = set(list(record_ids)[:10])
    except Exception:
        pass

print(json.dumps({"record_ids": list(record_ids)}))
