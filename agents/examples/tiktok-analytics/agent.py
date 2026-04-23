"""TikTok watch-history analytics query agent.

Queries the watch-history records, asks the LLM to summarise hashtag
themes and per-user statistics, then uploads a JSON report to the
Postgres-backed artifact store via the bridge.

Env vars (set by hivemind):
  BRIDGE_URL      - HTTP endpoint for the bridge server
  SESSION_TOKEN   - Bearer token for bridge auth
  QUERY_PROMPT    - The user's question (unused here; analysis is fixed)
  RUN_ID          - Current run ID (set when launched via /v1/query-agents/submit)
"""

import base64
import json
import os
import sys
from collections import Counter

import httpx

BRIDGE = os.environ["BRIDGE_URL"]
TOKEN = os.environ["SESSION_TOKEN"]
RUN_ID = os.environ.get("RUN_ID", "local")

client = httpx.Client(
    base_url=BRIDGE,
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=120,
)


def call_tool(name: str, args: dict) -> str:
    resp = client.post(f"/tools/{name}", json={"arguments": args})
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Tool {name} error: {data['error']}")
    return data["result"]


def llm_call(messages: list[dict], max_tokens: int = 4096) -> str:
    resp = client.post("/llm/chat", json={"messages": messages, "max_tokens": max_tokens})
    resp.raise_for_status()
    return resp.json()["content"]


def artifact_upload(filename: str, content: bytes, content_type: str = "application/json") -> dict | None:
    try:
        resp = client.post("/sandbox/artifact-upload", json={
            "filename": filename,
            "content_base64": base64.b64encode(content).decode(),
            "content_type": content_type,
        })
        if resp.status_code >= 400:
            print(f"[artifact-upload] failed ({resp.status_code}): {resp.text[:200]}", file=sys.stderr)
            return None
        return resp.json()
    except Exception as e:
        print(f"[artifact-upload] exception: {e}", file=sys.stderr)
        return None


def main():
    # ── Step 1: Fetch all watch-history rows ──
    # Cast UUID columns to text to avoid JSON serialization issues in the SQL proxy
    rows_json = call_tool("execute_sql", {
        "sql": (
            "SELECT video_id, title, author, hashtags, likes, views, "
            "shares, duration_ms, approx_times_watched, "
            "tiktok_account_id::text AS tiktok_account_id "
            "FROM data_xordi_tiktok_oauth_watch_history "
            "ORDER BY watched_at DESC"
        ),
    })

    # call_tool returns a JSON string; unwrap until we get a list
    rows = rows_json
    for _ in range(3):
        if isinstance(rows, str):
            rows = json.loads(rows)
        else:
            break
    if not isinstance(rows, list):
        print(f"ERROR: unexpected rows type: {type(rows).__name__}", file=sys.stderr)
        return

    total = len(rows)

    # ── Step 2: Basic statistics ──
    unique_users = len({r["tiktok_account_id"] for r in rows})
    unique_authors = len({r["author"] for r in rows if r.get("author")})

    all_hashtags = []
    for r in rows:
        raw = r.get("hashtags") or "[]"
        try:
            tags = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            tags = []
        if isinstance(tags, list):
            all_hashtags.extend(t.lower() for t in tags)

    tag_counts = Counter(all_hashtags)
    top_tags = tag_counts.most_common(20)

    stats = {
        "total_videos": total,
        "unique_users": unique_users,
        "unique_authors": unique_authors,
        "total_hashtags_used": len(all_hashtags),
        "unique_hashtags": len(tag_counts),
        "top_20_hashtags": [{"tag": t, "count": c} for t, c in top_tags],
    }

    # ── Step 3: LLM summarisation of hashtag themes ──
    llm_prompt = (
        "Here is a dataset of TikTok watch-history records.\n\n"
        f"Basic stats:\n{json.dumps(stats, indent=2)}\n\n"
        f"Sample records (first 10):\n{json.dumps(rows[:10], indent=2, ensure_ascii=False)}\n\n"
        "Please provide:\n"
        "1. A summary of the main content themes based on the hashtags and titles\n"
        "2. What categories of content this user group watches most\n"
        "3. Any interesting patterns (engagement, watch duration, etc.)\n"
        "Answer in a structured JSON with keys: themes, categories, patterns"
    )
    llm_answer = llm_call([
        {
            "role": "system",
            "content": (
                "You are a data analyst. Analyse the TikTok watch-history data and "
                "return a JSON object with keys: themes (list of strings), "
                "categories (list of strings), patterns (list of strings). "
                "Return ONLY valid JSON, no markdown fences."
            ),
        },
        {"role": "user", "content": llm_prompt},
    ])

    # Parse LLM JSON (strip markdown fences if present)
    cleaned = llm_answer.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        analysis = json.loads(cleaned)
    except json.JSONDecodeError:
        analysis = {"raw_response": llm_answer}

    # ── Step 4: Build final report ──
    report = {
        "run_id": RUN_ID,
        "statistics": stats,
        "llm_analysis": analysis,
    }

    # ── Step 5: Upload to artifact store (optional — gracefully skip on failure) ──
    report_bytes = json.dumps(report, indent=2, ensure_ascii=False).encode()
    upload = artifact_upload("report.json", report_bytes, "application/json")

    # Print result (captured as agent stdout by hivemind-core)
    result = {"status": "completed", "report": report}
    if upload:
        result["artifact_path"] = upload["path"]
        result["artifact_size_bytes"] = upload["size_bytes"]
        result["artifact_retention_seconds"] = upload["retention_seconds"]
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
