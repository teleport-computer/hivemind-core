"""Fail fast when someone tries to run the archived benchmark package."""

print(
    "autoresearch.legacy_bench is archived and targets removed APIs. "
    "Use eval/ for current room-agent evaluation.",
)
raise SystemExit(2)
