#!/usr/bin/env python3
"""Example custom indexing agent.

Receives:
- DOCUMENT_TEXT: the text being indexed
- Access to tools: search_index, read_record, list_index, list_by_user, list_users (scoped to user)
- Access to LLM: /llm/chat (optional, for summarization)

Outputs JSON to stdout:
{
  "title": "...",
  "summary": "...",
  "tags": ["...", "..."],
  "key_claims": ["...", "..."],
  "extra": {}
}

This example agent searches the user's existing documents to extract
consistent tags, then uses the LLM to generate a title and summary.
"""
import json
import os
import re
import urllib.request

BRIDGE = os.environ["BRIDGE_URL"]
TOKEN = os.environ["SESSION_TOKEN"]
DOC_TEXT = os.environ.get("DOCUMENT_TEXT", os.environ.get("PROMPT", ""))


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


# Extract capitalized words as search terms for finding similar docs
search_terms = " ".join(re.findall(r"\b[A-Z][a-z]+\b", DOC_TEXT[:500]))
if not search_terms:
    search_terms = " ".join(DOC_TEXT.split()[:10])

# Search for similar documents to extract consistent tags
existing_tags = set()
similar_count = 0
try:
    search_result = call(
        "POST", "/tools/search_index", {"arguments": {"query": search_terms, "limit": 5}}
    )
    results = json.loads(search_result.get("result", "[]"))
    similar_count = len(results)
    for item in results:
        tags_str = item.get("tags", "")
        existing_tags.update(t.strip() for t in tags_str.split(",") if t.strip())
except Exception:
    pass  # no existing docs — that's fine

# Use LLM to generate title, summary, and key_claims
try:
    context = f"Similar document tags in this user's collection: {', '.join(existing_tags)}" if existing_tags else ""
    llm_resp = call(
        "POST",
        "/llm/chat",
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Extract a structured index from the following text. "
                        "Return ONLY valid JSON with keys: title (string, under 100 chars), "
                        "summary (string, 2-3 sentences), tags (list of strings, 3-8 keywords), "
                        "key_claims (list of strings, factual assertions). "
                        f"{context}\n\nText:\n{DOC_TEXT[:5000]}"
                    ),
                }
            ],
            "max_tokens": 512,
        },
    )
    content = llm_resp.get("content", "")
    # Try to parse JSON from the LLM response
    try:
        index = json.loads(content)
    except json.JSONDecodeError:
        # Try extracting from markdown code fence
        import re as _re

        m = _re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, _re.DOTALL)
        if m:
            index = json.loads(m.group(1).strip())
        else:
            raise
except Exception:
    # Fallback: simple heuristic indexing
    first_line = DOC_TEXT.split("\n")[0] if "\n" in DOC_TEXT else DOC_TEXT[:100]
    index = {
        "title": first_line[:100],
        "summary": DOC_TEXT[:300] + ("..." if len(DOC_TEXT) > 300 else ""),
        "tags": list(existing_tags)[:8] if existing_tags else ["untagged"],
        "key_claims": [],
    }

# Merge in existing tags for consistency
if existing_tags:
    current_tags = set(index.get("tags", []))
    # Add relevant existing tags (keep total under 8)
    for tag in existing_tags:
        if len(current_tags) >= 8:
            break
        current_tags.add(tag)
    index["tags"] = list(current_tags)[:8]

index["extra"] = {"similar_docs": similar_count}

print(json.dumps(index))
