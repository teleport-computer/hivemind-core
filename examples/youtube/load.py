"""Load YouTube watch history into hivemind."""
import json, sys, httpx
from datetime import datetime, timezone

HIVEMIND_URL = "https://693d3fa15896bcff98d80cc67103e5ae54499890-8100.dstack-pha-prod7.phala.network"
API_KEY = "BAbqNXblGE9zqaIu7APlIvO17M3jfVvqmET1IXOER00"

headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

def store(sql, params=None):
    r = httpx.post(f"{HIVEMIND_URL}/v1/store", headers=headers,
                   json={"sql": sql, "params": params or []}, timeout=30)
    r.raise_for_status()
    return r.json()

store("""
CREATE TABLE IF NOT EXISTS watch_history (
    id SERIAL PRIMARY KEY,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    is_short BOOLEAN DEFAULT FALSE,
    views TEXT,
    watched_at TIMESTAMPTZ
)
""")
store("CREATE INDEX IF NOT EXISTS idx_wh_watched_at ON watch_history (watched_at)")
print("Table created.")

history = json.load(open(sys.argv[1]))
count = 0
for entry in history:
    ts = datetime.fromtimestamp(entry["date"] / 1000, tz=timezone.utc).isoformat()
    store(
        "INSERT INTO watch_history (video_id, title, url, is_short, views, watched_at) VALUES (%s, %s, %s, %s, %s, %s)",
        [entry.get("id", ""), entry.get("title", ""), entry.get("url", ""),
         entry.get("isShort", False), entry.get("views", ""), ts]
    )
    count += 1
    if count % 100 == 0:
        print(f"  {count}...")

print(f"Done. Loaded {count} records.")
