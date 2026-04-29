"""Deterministic query agent for top watch-history hashtags."""

import asyncio
import json
import os

import aiohttp


BRIDGE_URL = os.environ["BRIDGE_URL"]
SESSION_TOKEN = os.environ["SESSION_TOKEN"]


async def call_tool(name: str, arguments: dict) -> str:
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{BRIDGE_URL}/tools/{name}",
            json={"arguments": arguments},
            headers={"Authorization": f"Bearer {SESSION_TOKEN}"},
        ) as resp:
            data = await resp.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data.get("result", "")


def markdown_table(rows: list[dict]) -> str:
    lines = ["| rank | hashtag | watches |", "|---:|---|---:|"]
    for idx, row in enumerate(rows, 1):
        rank = row.get("rank") or idx
        hashtag = str(row.get("hashtag") or "").replace("|", "\\|")
        watches = row.get("watches") or row.get("watch_count") or row.get("count") or 0
        lines.append(f"| {rank} | {hashtag} | {watches} |")
    return "\n".join(lines)


async def main() -> None:
    sql = """
    WITH expanded AS (
      SELECT lower(trim(tag)) AS hashtag
      FROM watch_history
      CROSS JOIN LATERAL jsonb_array_elements_text(hashtags::jsonb) AS tag
      WHERE hashtags IS NOT NULL
    ),
    ranked AS (
      SELECT hashtag, count(*)::bigint AS watches
      FROM expanded
      WHERE hashtag <> ''
      GROUP BY hashtag
      HAVING count(*) >= 5
    )
    SELECT row_number() OVER (ORDER BY watches DESC, hashtag) AS rank,
           hashtag,
           watches
    FROM ranked
    ORDER BY watches DESC, hashtag
    LIMIT 30
    """
    raw = await call_tool("execute_sql", {"sql": sql, "params": []})
    data = json.loads(raw)
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        rows = data["rows"]
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    print(markdown_table(rows))


if __name__ == "__main__":
    asyncio.run(main())
