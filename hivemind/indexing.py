import json
import re

from openai import AsyncOpenAI

from .models import IndexEntry
from .prompts import INDEX_SYSTEM, build_hyde_prompt


def _extract_json(text: str) -> dict:
    """Extract JSON from a response that may be wrapped in markdown code fences."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1).strip())
    raise ValueError(f"Could not extract JSON from response: {text[:200]}")


async def generate_index(client: AsyncOpenAI, text: str, model: str) -> IndexEntry:
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": INDEX_SYSTEM},
            {"role": "user", "content": text},
        ],
        max_tokens=1024,
    )
    raw = resp.choices[0].message.content or "{}"
    data = _extract_json(raw)
    return IndexEntry(
        title=data.get("title", "Untitled"),
        summary=data.get("summary", ""),
        tags=data.get("tags", []),
        key_claims=data.get("key_claims", []),
    )


async def hyde_expand(
    client: AsyncOpenAI, question: str, context: str, model: str
) -> str:
    prompt = build_hyde_prompt(question, context)
    resp = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=256,
    )
    return resp.choices[0].message.content or question


