from __future__ import annotations

import json
import subprocess

import eval.cli as eval_cli


def test_extract_run_metrics_decodes_usage_and_stage_timings():
    metrics = eval_cli._extract_run_metrics(
        {
            "status": "completed",
            "billing_status": "settled",
            "billing_cost_micro_usd": "123",
            "created_at": 10.0,
            "updated_at": 15.25,
            "scope_started_at": 10.5,
            "scope_ended_at": 12.0,
            "query_started_at": 12.0,
            "query_ended_at": 15.0,
            "usage_json": json.dumps(
                {
                    "calls": 4,
                    "prompt_tokens": 1000,
                    "completion_tokens": 250,
                    "total_tokens": 1300,
                }
            ),
            "artifacts": [{"filename": "report.md"}, {"filename": "report.pdf"}],
        }
    )

    assert metrics["run_status"] == "completed"
    assert metrics["billing_status"] == "settled"
    assert metrics["billing_cost_micro_usd"] == 123
    assert metrics["llm_calls"] == 4
    assert metrics["total_tokens"] == 1300
    assert metrics["duration_seconds"] == 5.25
    assert metrics["stage_seconds"] == {"scope": 1.5, "query": 3.0}
    assert metrics["telemetry_artifact_count"] == 2


def test_run_room_persists_final_run_telemetry(tmp_path, monkeypatch):
    calls = []
    output = (
        "# Executive Summary\n"
        "Aggregate watch-history evidence shows a defensible pattern.\n\n"
        "| Metric | Value |\n| --- | --- |\n| Example aggregate | 12 |\n\n"
        "## Methodology and assumptions\n"
        "Used scoped aggregate queries only.\n\n"
        "## Privacy note\n"
        "Only aggregate evidence is reported.\n\n"
        "## Limitations\n"
        "This is a compact canary report.\n\n"
        "## Product implications\n"
        "The pattern can inform product decisions."
    )

    def fake_run(cmd, *, text, capture_output, timeout):
        calls.append(cmd)
        if "ask" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {"run_id": "run-123", "output": output, "artifacts": []}
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {
                    "run_id": "run-123",
                    "status": "completed",
                    "billing_status": "settled",
                    "billing_cost_micro_usd": 456,
                    "created_at": 20.0,
                    "updated_at": 30.0,
                    "usage_json": {
                        "calls": 3,
                        "prompt_tokens": 100,
                        "completion_tokens": 40,
                    },
                    "artifacts": [{"filename": "report.md"}],
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("HMCTL_BIN", "hmctl")
    monkeypatch.setattr(eval_cli.subprocess, "run", fake_run)

    parser = eval_cli.build_parser()
    args = parser.parse_args(
        [
            "run-room",
            "watch_history_report_artifact",
            "room-abc",
            "--model",
            "openai/gpt-5",
            "--hmctl-profile",
            "prod",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert eval_cli._cmd_run_room(args) == 0

    ask_cmd, telemetry_cmd = calls
    assert ask_cmd[:3] == ["hmctl", "--profile", "prod"]
    assert telemetry_cmd[:3] == ["hmctl", "--profile", "prod"]
    assert telemetry_cmd[-2:] == ["run-123", "--json"]

    summary = json.loads(
        (tmp_path / "watch_history_report_artifact__summary.json").read_text()
    )
    assert summary[0]["run_id"] == "run-123"
    assert summary[0]["passed"] is True
    assert summary[0]["billing_cost_micro_usd"] == 456
    assert summary[0]["llm_calls"] == 3
    assert summary[0]["total_tokens"] == 140
    assert summary[0]["artifact_count"] == 1
    assert summary[0]["telemetry_artifact_count"] == 1
    assert (
        tmp_path / "watch_history_report_artifact__openai_gpt-5__run.json"
    ).exists()
