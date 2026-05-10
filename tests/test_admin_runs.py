from __future__ import annotations

import json

from hivemind.api.admin_runs import _enrich_run


def test_enrich_run_uses_pipeline_calls_and_total_tokens():
    run = {
        "room_id": "room-1",
        "usage_json": json.dumps(
            {
                "calls": "7",
                "prompt_tokens": 100,
                "completion_tokens": 25,
                "total_tokens": 140,
            }
        ),
    }

    enriched = _enrich_run(
        run,
        tenant_id="tenant-1",
        tenant_name="Tenant One",
        room_name_by_id={"room-1": "Watch History"},
    )

    assert enriched["tenant_id"] == "tenant-1"
    assert enriched["tenant_name"] == "Tenant One"
    assert enriched["room_name"] == "Watch History"
    assert enriched["llm_calls"] == 7
    assert enriched["prompt_tokens"] == 100
    assert enriched["completion_tokens"] == 25
    assert enriched["total_tokens"] == 140
    assert "usage_json" not in enriched


def test_enrich_run_falls_back_to_prompt_plus_completion_tokens():
    run = {
        "room_id": "room-1",
        "usage_json": {"llm_calls": 2, "prompt_tokens": 30, "completion_tokens": 4},
    }

    enriched = _enrich_run(run, "tenant-1", "Tenant One", {})

    assert enriched["llm_calls"] == 2
    assert enriched["total_tokens"] == 34
