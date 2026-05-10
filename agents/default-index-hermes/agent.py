"""Default index agent — Hermes harness.

Same role as agents/default-index/agent.py: ingest a document, emit a
structured retrieval index. May consult the database for cross-document
consistency. Falls back to a heuristic index on agent failure.

Output JSON to stdout: {"index_text": "...", "metadata": {...}}

Env (set automatically by the sandbox runner):
  BRIDGE_URL, SESSION_TOKEN  — bridge connection
  HIVEMIND_AGENT_ROLE=index  — plugin registers execute_sql + get_schema
  HIVEMIND_MODEL             — model id passed to AIAgent
  DOCUMENT_DATA              — raw document content
  DOCUMENT_METADATA          — existing metadata JSON (optional)
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_PLUGINS_DIR = os.environ.get("HERMES_BUNDLED_PLUGINS", "/opt/hivemind/plugins")
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)


def _isolate_hivemind_toolset() -> None:
    """Keep Hermes startup from importing unrelated built-in tool modules."""
    if os.environ.get("HIVEMIND_HERMES_ENABLE_BUILTIN_TOOLS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    try:
        from tools import registry as hermes_registry  # type: ignore
    except Exception:
        return
    hermes_registry.discover_builtin_tools = lambda *args, **kwargs: []


_isolate_hivemind_toolset()
import hivemind  # noqa: E402, F401

from run_agent import AIAgent  # noqa: E402

DOCUMENT_DATA = os.environ.get("DOCUMENT_DATA", "")
DOCUMENT_METADATA = os.environ.get("DOCUMENT_METADATA", "{}")
HIVEMIND_MODEL = os.environ.get("HIVEMIND_MODEL", "moonshotai/kimi-2.6")

DEFAULT_SYSTEM_PROMPT = """\
You are an indexing agent. Build a high-quality retrieval index for the
provided document.

You have two tools:
- execute_sql: Query existing data for cross-document consistency.
- get_schema: Return the database schema.

No external network. No file or shell tools.

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

_PROMPT_FILE = Path("/app/prompt.md")
if _PROMPT_FILE.exists():
    SYSTEM_PROMPT = _PROMPT_FILE.read_text()
else:
    SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


# ── Heuristic fallback (mirrors agents/default-index/agent.py) ──


def _heuristic_index(text: str) -> dict:
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

    claims: list[str] = []
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


def main() -> None:
    if not DOCUMENT_DATA.strip():
        print(json.dumps({"index_text": "", "metadata": {}}))
        return

    try:
        existing_metadata = json.loads(DOCUMENT_METADATA)
        if not isinstance(existing_metadata, dict):
            existing_metadata = {}
    except json.JSONDecodeError:
        existing_metadata = {}

    body = (
        "Create a structured index for this document.\n\n"
        f"DOCUMENT:\n{DOCUMENT_DATA[:20_000]}\n\n"
        "Use tools only if needed (for cross-document consistency). "
        "Return strict JSON with keys: title, summary, tags, key_claims."
    )

    base_url = os.environ["BRIDGE_URL"].rstrip("/") + "/v1"
    api_key = os.environ["SESSION_TOKEN"]

    raw_index: dict | None = None
    try:
        agent = AIAgent(
            base_url=base_url,
            api_key=api_key,
            provider="custom",
            model=HIVEMIND_MODEL,
            max_iterations=6,
            enabled_toolsets=["hivemind"],
            ephemeral_system_prompt=SYSTEM_PROMPT,
            skip_context_files=True,
            skip_memory=True,
            quiet_mode=True,
            save_trajectories=False,
        )
        response = agent.chat(body) or ""
        if response:
            raw_index = _extract_json_obj(response)
    except Exception as e:
        print(
            f"AIAgent error, falling back to heuristic: {e}",
            file=sys.stderr,
        )

    if raw_index is None:
        raw_index = _heuristic_index(DOCUMENT_DATA)

    index = _normalize_index(raw_index, DOCUMENT_DATA)
    index_text = _build_index_text(index)

    metadata = dict(existing_metadata)
    metadata.update(index)

    print(json.dumps({"index_text": index_text, "metadata": metadata}))


if __name__ == "__main__":
    main()
