"""Deterministic scope agent for aggregate watch-history rooms."""

import json
import textwrap


SCOPE_FN = r"""
def scope(sql, params, rows):
    if not isinstance(rows, list):
        rows = []

    sensitive = (
        "sec_user_id", "video_id", "url", "title", "description", "author_id",
    )
    group_keys = (
        "hashtag", "tag", "author", "music", "day", "week", "month",
        "bucket", "watched_day", "watched_week", "watched_month",
    )
    count_keys = (
        "watches", "watch_count", "count", "n", "count_star", "match_count",
    )

    cleaned = []
    saw_sensitive = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        clean = {}
        for key, value in row.items():
            lower_key = str(key).lower()
            if lower_key in sensitive:
                clean[key] = "[redacted]"
                saw_sensitive = True
            else:
                clean[key] = value
        cleaned.append(clean)

    if not cleaned:
        return {"allow": True, "rows": []}

    def count_value(row):
        for key in count_keys:
            if key in row:
                try:
                    return int(row.get(key) or 0)
                except Exception:
                    return None
        for key, value in row.items():
            lower_key = str(key).lower()
            if "count" in lower_key or "watch" in lower_key:
                try:
                    return int(value or 0)
                except Exception:
                    return None
        return None

    aggregate_like = True
    for row in cleaned:
        lowered = [str(key).lower() for key in row.keys()]
        has_group = any(
            key in group_keys or "hashtag" in key
            for key in lowered
        )
        has_count = count_value(row) is not None
        allowed_columns = all(
            key in group_keys
            or key in count_keys
            or key == "rank"
            or "count" in key
            or "watch" in key
            or "hashtag" in key
            for key in lowered
        )
        single_aggregate = len(cleaned) == 1 and has_count and not saw_sensitive
        if not ((has_group and has_count) or single_aggregate) or not allowed_columns:
            aggregate_like = False

    if aggregate_like:
        filtered = []
        for row in cleaned:
            value = count_value(row)
            if value is None or value >= 5:
                filtered.append(row)
        return {"allow": True, "rows": filtered[:50]}

    if len(cleaned) == 1 and not saw_sensitive:
        return {"allow": True, "rows": cleaned}

    return {"allow": True, "rows": [{"match_count": len(cleaned)}]}
"""


print(json.dumps({"scope_fn": textwrap.dedent(SCOPE_FN).strip()}))
