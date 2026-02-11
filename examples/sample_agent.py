#!/usr/bin/env python3
"""
Sample hivemind sandbox agent.

This agent receives a question via the PROMPT env var, searches the
knowledge base via the bridge, asks the LLM to synthesize an answer,
and prints the result to stdout.

No external dependencies — uses only the Python standard library.

Protocol:
  - BRIDGE_URL: HTTP URL of the bridge server
  - SESSION_TOKEN: Bearer token for bridge auth
  - PROMPT: The question to answer
  - stdout: Final answer (captured by hivemind)
"""

import json
import os
import sys
import urllib.request

BRIDGE = os.environ["BRIDGE_URL"]
TOKEN = os.environ["SESSION_TOKEN"]
PROMPT = os.environ.get("PROMPT", "")


def call(method, path, body=None):
    """Make an HTTP request to the bridge."""
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
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    # 1. Search for relevant records
    search_result = call(
        "POST",
        "/tools/search_index",
        {"arguments": {"query": PROMPT, "limit": 10}},
    )
    records = search_result.get("result", "[]")

    # 2. Parse search results and read top records
    try:
        results = json.loads(records)
    except (json.JSONDecodeError, TypeError):
        results = []

    record_texts = []
    for r in results[:5]:  # read up to 5 records
        rid = r.get("record_id", "")
        if rid:
            read_result = call(
                "POST",
                "/tools/read_record",
                {"arguments": {"record_id": rid}},
            )
            text = read_result.get("result", "")
            if text and text != "Record not found":
                record_texts.append(f"[{rid}]: {text[:2000]}")

    # 3. Ask the LLM to synthesize an answer
    if record_texts:
        context = "\n\n".join(record_texts)
        llm_prompt = (
            f"Based on these records from the knowledge base:\n\n"
            f"{context}\n\n"
            f"Answer this question in your own words (paraphrase, "
            f"don't copy verbatim): {PROMPT}"
        )
    else:
        llm_prompt = (
            f"I searched the knowledge base but found no relevant records. "
            f"The question was: {PROMPT}\n\n"
            f"Please respond that no relevant information was found."
        )

    resp = call(
        "POST",
        "/llm/chat",
        {"messages": [{"role": "user", "content": llm_prompt}]},
    )

    # 4. Output the answer
    print(resp.get("content", "(No response from LLM)"))


if __name__ == "__main__":
    main()
