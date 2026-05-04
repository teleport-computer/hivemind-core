"""Bob's sealed query agent — bilateral private-data dinner negotiation.

This agent is the participant side of the dinner-negotiation example.
It runs inside a Hivemind CVM and has access to:

  1. Bob's own private files, bundled into this archive at upload time.
     Sealed at rest with a key derivable only inside the CVM, so Alice
     and the operator cannot read them. Inside the CVM the container
     decrypts them transparently and reads them like any local file.

       b-private/my-calendar.json     — Bob's busy/free schedule
       b-private/my-preferences.json  — neighborhood / dietary prefs

  2. Alice's calendar via SQL through the bridge. The room's scope
     agent has already designed a filter (`scope_fn`) that limits
     Bob's view to whatever the rules permit — typically only
     start_time/end_time of free slots in the requested window.

The agent computes the intersection of free slots, filters venue
options against Bob's preferences, and asks the LLM to produce one
date+time+venue+one-sentence justification. The mediator agent then
audits and releases.

Env vars set by hivemind:
  BRIDGE_URL    — HTTP endpoint for the bridge (LLM + tools)
  SESSION_TOKEN — Bearer token for the bridge
  QUERY_PROMPT  — Bob's question (e.g., "Find a Thu/Fri evening near Mission")
"""

import json
import os
from pathlib import Path

import httpx

BRIDGE = os.environ["BRIDGE_URL"]
TOKEN = os.environ["SESSION_TOKEN"]
PROMPT = os.environ.get("QUERY_PROMPT", "").strip()

PRIVATE_DIR = Path(__file__).parent / "b-private"

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
    resp = client.post(
        "/llm/chat",
        json={"messages": messages, "max_tokens": max_tokens},
    )
    resp.raise_for_status()
    return resp.json()["content"]


def load_private(name: str) -> dict:
    """Load a bundled private file. Bytes are sealed at rest; this read
    only works inside the CVM, where the per-room key is available."""
    path = PRIVATE_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"private file {name} missing — was the agent uploaded with "
            f"inspection_mode=sealed and the b-private/ directory included?"
        )
    return json.loads(path.read_text())


def main():
    if not PROMPT:
        print("No question provided.")
        return

    # ── 1. Read Bob's private state from the bundle. ────────────────
    my_calendar = load_private("my-calendar.json")
    my_prefs = load_private("my-preferences.json")

    # ── 2. Inspect Alice's schema, then SELECT only what scope allows. ──
    schema = call_tool("get_schema", {})

    # We deliberately ask for a narrow projection — only times. The
    # scope agent should already enforce this, but asking narrowly
    # makes the agent's intent unambiguous and gives the mediator less
    # to redact if scope_fn is permissive.
    sql = (
        "SELECT start_time, end_time FROM calendar "
        "WHERE is_busy = false "
        "  AND start_time >= '2026-05-04' "
        "  AND start_time <  '2026-05-15' "
        "ORDER BY start_time"
    )
    alice_free_raw = call_tool("execute_sql", {"sql": sql})
    try:
        alice_free = json.loads(alice_free_raw)
    except json.JSONDecodeError:
        alice_free = []

    # ── 3. Compute intersection in plain Python before the LLM call. ──
    # Doing the join deterministically here means the LLM only has to
    # do the polish step, not the arithmetic. Smaller LLM context, less
    # to redact, more reproducible answers.
    bob_free = [
        e for e in my_calendar.get("events", []) if not e.get("busy")
    ]
    candidates = []
    for a in alice_free:
        a_date = (a.get("start_time") or "")[:10]
        for b in bob_free:
            if b.get("date") != a_date:
                continue
            # Both free on the same day; let the LLM pick the time +
            # venue + sentence. We don't need full overlap arithmetic
            # here for the demo.
            candidates.append({
                "date": a_date,
                "alice_window": [
                    (a.get("start_time") or "")[11:16],
                    (a.get("end_time") or "")[11:16],
                ],
                "bob_window": [b.get("start"), b.get("end")],
            })

    if not candidates:
        print("No overlapping free slot in the requested window.")
        return

    # ── 4. LLM does the natural-language polish + venue suggestion. ──
    # Critically, we hand the LLM only what it needs to produce the
    # final sentence. We don't echo our private files into the prompt
    # beyond the relevant subset, so the mediator has less to scrub.
    system = (
        "You are a scheduling agent producing exactly one short answer:\n"
        "  one date and time, one venue (name + neighborhood), and one "
        "  short justification sentence.\n"
        "Do NOT list alternatives. Do NOT mention any time other than "
        "the chosen one. Do NOT mention any venue the user has visited "
        "recently. Do NOT include any flags or preferences verbatim."
    )
    user = (
        f"User question: {PROMPT}\n\n"
        f"Candidate dates where both Alice and Bob are free:\n"
        f"{json.dumps(candidates, indent=2)}\n\n"
        f"Bob's neighborhood preferences (use, do not echo):\n"
        f"  preferred: {my_prefs['neighborhoods_preferred']}\n"
        f"  avoid:     {my_prefs['neighborhoods_avoid']}\n"
        f"  diet:      avoid_pasta={my_prefs['diet']['avoid_pasta']}, "
        f"vegetarian={my_prefs['diet']['vegetarian']}\n"
        f"Recently visited (avoid suggesting these):\n"
        f"  {[v['name'] for v in my_prefs['venues_visited_recently']]}\n\n"
        f"Pick the earliest candidate that fits, suggest one venue that "
        f"matches neighborhood + diet preferences, write one short answer."
    )
    answer = llm_call([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], max_tokens=400)

    # The mediator will see this stdout as RAW_OUTPUT and decide what
    # to release. Anything that violates rules.md gets stripped.
    print(answer.strip())


if __name__ == "__main__":
    main()
