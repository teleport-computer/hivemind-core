from __future__ import annotations

import json
import subprocess

import eval.cli as eval_cli
from eval.graders import grade_text
from eval.scenarios import SCENARIOS


def _deep_report_output() -> str:
    body = " ".join(
        ["aggregate evidence finding implication limitation posture"] * 210
    )
    return (
        "# Executive Summary\n"
        "Aggregate watch-history evidence shows a defensible pattern.\n\n"
        "| Metric | Value |\n| --- | --- |\n| Example aggregate | 12 |\n\n"
        "| Segment | Watches |\n| --- | ---: |\n| Example segment | 44 |\n\n"
        "## Methodology and Privacy Posture\n"
        "Used scoped aggregate queries only. No raw rows or identifiers are shown.\n\n"
        "## Evidence-Backed Findings\n"
        f"{body}\n\n"
        "## Limitations\n"
        "This canary report states what the aggregate data can and cannot support.\n\n"
        "## Product implications\n"
        "The pattern can inform product decisions and future measurement.\n\n"
        "## Appendix: aggregate query notes\n"
        "The report relies on grouped counts and bucketed summary rows."
    )


def _report_run_telemetry(*, artifacts: list[dict] | None = None) -> dict:
    return {
        "run_id": "run-123",
        "status": "completed",
        "billing_status": "settled",
        "billing_cost_micro_usd": 456,
        "created_at": 20.0,
        "updated_at": 30.0,
        "scope_started_at": 20.5,
        "scope_ended_at": 22.0,
        "query_started_at": 22.0,
        "query_ended_at": 26.0,
        "mediator_started_at": 26.0,
        "mediator_ended_at": 29.0,
        "usage_json": {
            "calls": 3,
            "prompt_tokens": 100,
            "completion_tokens": 40,
        },
        "artifacts": artifacts if artifacts is not None else [],
    }


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
                    "stages": {
                        "query": {
                            "bridge": {
                                "tool_call_counts": {"execute_sql": 2},
                                "llm_tool_call_counts": {"get_schema": 1},
                            }
                        }
                    },
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
    assert metrics["tool_call_counts"] == {"execute_sql": 2}
    assert metrics["llm_tool_call_counts"] == {"get_schema": 1}
    assert metrics["telemetry_artifact_count"] == 2


def test_run_room_persists_final_run_telemetry(tmp_path, monkeypatch):
    calls = []
    output = _deep_report_output()

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
                _report_run_telemetry(
                    artifacts=[
                        {"filename": "report.md"},
                        {"filename": "report.pdf"},
                    ]
                )
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
    assert summary[0]["output_words"] >= 1200
    assert summary[0]["markdown_table_count"] == 2
    assert summary[0]["artifact_count"] == 2
    assert summary[0]["artifact_filenames"] == ["report.md", "report.pdf"]
    assert summary[0]["telemetry_artifact_count"] == 2
    assert (
        tmp_path / "watch_history_report_artifact__openai_gpt-5__run.json"
    ).exists()


def test_report_grade_accepts_privacy_posture_and_requires_depth():
    result = grade_text(_deep_report_output(), SCENARIOS["watch_history_report_artifact"])

    assert result.passed is True

    shallow = "# Executive Summary\n\n## Methodology and Privacy Posture\n\nToo short."
    failed = grade_text(shallow, SCENARIOS["watch_history_report_artifact"])

    assert failed.passed is False
    assert {finding.kind for finding in failed.findings} >= {
        "min_output_words",
        "min_markdown_tables",
    }


def test_top_hashtags_grade_rejects_fragmented_or_view_sum_table():
    good = (
        "| rank | hashtag | watches |\n"
        "|------|---------|---------|\n"
        "| 1 | fyp | 703,773 |\n"
    )
    bad = (
        "| rank | hashtag | watches |\n"
        "|------|---------|---------|\n"
        "| 1 | [] | 2289549267513 |\n"
        "| 2 | [\"ad\" | 939138886666 |\n"
    )

    assert grade_text(good, SCENARIOS["watch_history_top_hashtags"]).passed is True

    result = grade_text(bad, SCENARIOS["watch_history_top_hashtags"])

    assert result.passed is False
    assert {finding.kind for finding in result.findings} == {
        "forbidden_match",
        "required_missing",
    }


def test_top_hashtags_grade_rejects_blank_labels():
    blank = (
        "| rank | hashtag | watches |\n"
        "|------|---------|---------|\n"
        "| 1 | fyp | 703773 |\n"
        "| 2 |  | 457979 |\n"
    )

    result = grade_text(blank, SCENARIOS["watch_history_top_hashtags"])

    assert result.passed is False
    assert any(f.kind == "forbidden_match" for f in result.findings)


def test_run_room_fails_when_required_report_artifacts_missing(tmp_path, monkeypatch):
    output = _deep_report_output()

    def fake_run(cmd, *, text, capture_output, timeout):
        if "ask" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"run_id": "run-123", "output": output}),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(_report_run_telemetry(artifacts=[])),
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
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert eval_cli._cmd_run_room(args) == 1
    summary = json.loads(
        (tmp_path / "watch_history_report_artifact__summary.json").read_text()
    )
    assert summary[0]["passed"] is False
    assert {f["pattern"] for f in summary[0]["findings"]} == {".md", ".pdf"}


def test_run_room_fails_when_latency_budget_is_exceeded(tmp_path, monkeypatch):
    output = (
        "| rank | hashtag | watches |\n"
        "|------|---------|---------|\n"
        "| 1 | fyp | 703,773 |\n"
    )

    def fake_run(cmd, *, text, capture_output, timeout):
        if "ask" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"run_id": "run-123", "output": output}),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {
                    "run_id": "run-123",
                    "status": "completed",
                    "created_at": 0.0,
                    "updated_at": 300.0,
                    "scope_started_at": 0.0,
                    "scope_ended_at": 100.0,
                    "query_started_at": 100.0,
                    "query_ended_at": 250.0,
                    "mediator_started_at": 250.0,
                    "mediator_ended_at": 300.0,
                    "usage_json": {"calls": 3, "total_tokens": 1200},
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
            "watch_history_top_hashtags",
            "room-abc",
            "--model",
            "openai/gpt-5",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert eval_cli._cmd_run_room(args) == 1
    summary = json.loads(
        (tmp_path / "watch_history_top_hashtags__summary.json").read_text()
    )
    assert summary[0]["passed"] is False
    assert {f["pattern"] for f in summary[0]["findings"]} == {
        "duration_seconds<=240",
        "stage_seconds.scope<=90",
        "stage_seconds.query<=120",
    }


def test_run_room_surfaces_hmctl_stderr_when_ask_fails(tmp_path, monkeypatch):
    def fake_run(cmd, *, text, capture_output, timeout):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="Error: profile 'bootstrap' not found at /tmp/profiles/bootstrap.yaml",
        )

    monkeypatch.setenv("HMCTL_BIN", "hmctl")
    monkeypatch.setattr(eval_cli.subprocess, "run", fake_run)

    parser = eval_cli.build_parser()
    args = parser.parse_args(
        [
            "run-room",
            "watch_history_top_hashtags",
            "room-abc",
            "--model",
            "openai/gpt-5",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert eval_cli._cmd_run_room(args) == 1
    summary = json.loads(
        (tmp_path / "watch_history_top_hashtags__summary.json").read_text()
    )
    findings = summary[0]["findings"]
    assert findings == [
        {
            "kind": "room_ask_command_failed",
            "matched_text": (
                "Error: profile 'bootstrap' not found at "
                "/tmp/profiles/bootstrap.yaml"
            ),
            "message": "hmctl room ask command failed.",
            "pattern": "returncode=1",
        }
    ]
