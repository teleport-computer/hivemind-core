"""Default index agent — fully autonomous Claude Code with SQL tools.

Processes incoming data and writes structured indexes to the database.

Env vars (set automatically by the sandbox):
  BRIDGE_URL, SESSION_TOKEN — bridge connection
  ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY — SDK routes LLM calls through bridge
  DOCUMENT_DATA — raw document content to index
  DOCUMENT_METADATA — existing metadata JSON

Output JSON to stdout:
  {"index_text": "...", "metadata": {...}}
"""

import asyncio
import json
import os
import re
import sys

from claude_agent_sdk import ClaudeAgentOptions, query
from _bridge import create_hivemind_server

DOCUMENT_DATA = os.environ.get("DOCUMENT_DATA", "")
DOCUMENT_METADATA = os.environ.get("DOCUMENT_METADATA", "{}")

SYSTEM_PROMPT = """\
You are an indexing agent. Build a high-quality retrieval index for the provided document.

You have MCP tools to access the database:
- mcp__hivemind__execute_sql: Query existing data for cross-document consistency.
- mcp__hivemind__get_schema: Get the database schema.

You also have local Claude Code tools (Bash, Read, Write, Grep, Glob) \
available inside your container. Note: there is NO external network access — \
tools like WebSearch and WebFetch will not work. Use MCP tools for all data access.

Output ONLY valid JSON with this exact schema:
{
  "title": "<string, <= 100 chars>",
  "summary": "<string, 2-3 sentences>",
  "tags": ["<string>", "..."],
  "key_claims": ["<string>", "..."]
}

Rules:
- Maximum 8 tags, maximum 12 key_claims.
- Ground claims in the document content.
- Prefer factual, retrieval-friendly phrasing.
- Output ONLY the JSON object, nothing else.
"""

server = create_hivemind_server()


def _heuristic_index(text: str) -> dict:
    """Fallback: extract index fields heuristically from raw text."""
    stripped = text.strip()
    first_line = stripped.splitlines()[0] if stripped else "Untitled"
    title = " ".join(first_line.split())[:100] or "Untitled"

    sentence_candidates = re.split(r"(?<=[.!?])\s+", stripped)
    summary = " ".join(sentence_candidates[:2]).strip()
    if not summary:
        summary = stripped[:300]

    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", stripped.lower())
    counts: dict[str, int] = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    tags = [w for w, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:6]]

    claims = []
    for s in sentence_candidates:
        sentence = s.strip()
        if len(sentence) >= 24:
            claims.append(sentence[:220])
        if len(claims) >= 6:
            break

    return {"title": title, "summary": summary, "tags": tags, "key_claims": claims}


def _normalize_list(value, max_items: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = " ".join(item.strip().split())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= max_items:
            break
    return out


def _normalize_index(raw: dict, fallback_text: str) -> dict:
    fallback = _heuristic_index(fallback_text)

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        title = fallback["title"]
    title = " ".join(title.strip().split())[:100]

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = fallback["summary"]
    summary = " ".join(summary.strip().split())

    tags = _normalize_list(raw.get("tags"), max_items=8)
    if not tags:
        tags = fallback["tags"]

    claims = _normalize_list(raw.get("key_claims"), max_items=12)
    if not claims:
        claims = fallback["key_claims"]

    return {
        "title": title or "Untitled",
        "summary": summary,
        "tags": tags,
        "key_claims": claims,
    }


def _build_index_text(index: dict) -> str:
    parts = [index.get("title", ""), index.get("summary", "")]
    tags = index.get("tags", [])
    claims = index.get("key_claims", [])
    if tags:
        parts.append(" ".join(tags))
    if claims:
        parts.append(" ".join(claims))
    return "\n".join(p for p in parts if p).strip()


def _extract_json_obj(text: str) -> dict | None:
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if len(lines) >= 3 and lines[-1].strip() == "```":
            t = "\n".join(lines[1:-1]).strip()
        else:
            t = "\n".join(lines[1:]).strip()
    try:
        parsed = json.loads(t)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    # Find first balanced JSON object
    for i, ch in enumerate(t):
        if ch != "{":
            continue
        depth = 0
        for j in range(i, len(t)):
            if t[j] == "{":
                depth += 1
            elif t[j] == "}":
                depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(t[i : j + 1])
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass
                break
    return None


async def main() -> None:
    if not DOCUMENT_DATA.strip():
        print(json.dumps({"index_text": "", "metadata": {}}))
        return

    try:
        existing_metadata = json.loads(DOCUMENT_METADATA)
        if not isinstance(existing_metadata, dict):
            existing_metadata = {}
    except json.JSONDecodeError:
        existing_metadata = {}

    prompt = (
        "Create a structured index for this document.\n\n"
        f"DOCUMENT:\n{DOCUMENT_DATA[:20_000]}\n\n"
        "Use tools only if needed (for cross-document consistency). "
        "Return strict JSON with keys: title, summary, tags, key_claims."
    )

    raw_index = None
    final_result = ""

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                system_prompt=SYSTEM_PROMPT,
                mcp_servers={"hivemind": server},
                permission_mode="bypassPermissions",
                cwd="/tmp",
            ),
        ):
            if hasattr(message, "result"):
                final_result = message.result

        if final_result:
            raw_index = _extract_json_obj(final_result)
    except Exception as e:
        print(f"Agent SDK error, falling back to heuristic: {e}", file=sys.stderr)

    if raw_index is None:
        raw_index = _heuristic_index(DOCUMENT_DATA)

    index = _normalize_index(raw_index, DOCUMENT_DATA)
    index_text = _build_index_text(index)

    metadata = dict(existing_metadata)
    metadata.update(index)

    print(json.dumps({"index_text": index_text, "metadata": metadata}))


if __name__ == "__main__":
    asyncio.run(main())
