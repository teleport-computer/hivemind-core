from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import yaml

from .graders import count_markdown_tables, count_words, grade_text
from .scenarios import SCENARIOS
from .types import GradeFinding, RubricCheck, Scenario


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _cmd_list() -> int:
    for scenario in SCENARIOS.values():
        demands = ", ".join(scenario.superpower_demand) or "none"
        print(f"{scenario.id}\t{scenario.title}\t{demands}")
    return 0


def _cmd_grade(args: argparse.Namespace) -> int:
    scenario = SCENARIOS[args.scenario]
    output = _read_text(args.output)
    result = grade_text(output, scenario)
    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    return 0 if result.passed else 1


def _hmctl_base(profile: str | None = None) -> list[str]:
    cmd = shlex.split(os.environ.get("HMCTL_BIN", "uv run hmctl"))
    if profile:
        cmd.extend(["--profile", profile])
    return cmd


def _profile_name(profile: str | None) -> str:
    if profile:
        return profile
    if env_profile := os.environ.get("HIVEMIND_PROFILE", "").strip():
        return env_profile
    active_path = Path.home() / ".hivemind" / "active"
    try:
        active = active_path.read_text(encoding="utf-8").strip()
    except OSError:
        active = ""
    return active or "default"


def _profile_api_key(profile: str | None) -> str:
    profile_path = Path.home() / ".hivemind" / "profiles" / f"{_profile_name(profile)}.yaml"
    try:
        config = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    except OSError:
        return ""
    except yaml.YAMLError:
        return ""
    api_key = str(config.get("api_key") or "").strip()
    return api_key


def _hmroom_run_headers(room_ref: str, profile: str | None) -> tuple[str, dict] | None:
    if not room_ref.startswith("hmroom://"):
        return None
    parsed = urlparse(room_ref)
    qs = parse_qs(parsed.query)
    service = unquote((qs.get("service") or [""])[0]).rstrip("/")
    token = unquote((qs.get("token") or qs.get("share") or [""])[0])
    if not service or not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    api_key = _profile_api_key(profile)
    if api_key.startswith("hmk_"):
        headers["X-Hivemind-Api-Key"] = api_key
    return service, headers


def _fetch_hmroom_run_telemetry(
    room_ref: str,
    run_id: str,
    profile: str | None,
) -> subprocess.CompletedProcess | None:
    resolved = _hmroom_run_headers(room_ref, profile)
    if resolved is None:
        return None
    service, headers = resolved
    cmd = ["GET", f"{service}/v1/runs/{run_id}"]
    try:
        resp = httpx.get(
            f"{service}/v1/runs/{run_id}",
            headers=headers,
            timeout=60,
        )
    except httpx.HTTPError as e:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"{type(e).__name__}: {e}")
    if resp.status_code >= 400:
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=resp.text,
            stderr=f"HTTP {resp.status_code}: {resp.text[:500]}",
        )
    return subprocess.CompletedProcess(cmd, 0, stdout=resp.text, stderr="")


def _stage_seconds(run: dict, stage: str) -> float | None:
    started = run.get(f"{stage}_started_at")
    ended = run.get(f"{stage}_ended_at")
    try:
        if started is None or ended is None:
            return None
        return round(float(ended) - float(started), 3)
    except (TypeError, ValueError):
        return None


def _run_seconds(run: dict) -> float | None:
    try:
        return round(float(run["updated_at"]) - float(run["created_at"]), 3)
    except (KeyError, TypeError, ValueError):
        return None


def _coerce_usage(run: dict) -> dict:
    usage = run.get("usage") or run.get("usage_json") or {}
    if isinstance(usage, str):
        try:
            parsed = json.loads(usage)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return usage if isinstance(usage, dict) else {}


def _merge_counts(dst: dict[str, int], src: object) -> None:
    if not isinstance(src, dict):
        return
    for key, value in src.items():
        dst[str(key)] = dst.get(str(key), 0) + _int_value(value)


def _extract_bridge_counts(usage: dict) -> tuple[dict[str, int], dict[str, int]]:
    tool_counts: dict[str, int] = {}
    llm_tool_counts: dict[str, int] = {}
    bridge = usage.get("bridge") if isinstance(usage, dict) else {}
    if isinstance(bridge, dict):
        _merge_counts(tool_counts, bridge.get("tool_call_counts"))
        _merge_counts(llm_tool_counts, bridge.get("llm_tool_call_counts"))
    stages = usage.get("stages") if isinstance(usage, dict) else {}
    if isinstance(stages, dict):
        for stage in stages.values():
            if not isinstance(stage, dict):
                continue
            stage_bridge = stage.get("bridge") or {}
            if isinstance(stage_bridge, dict):
                _merge_counts(tool_counts, stage_bridge.get("tool_call_counts"))
                _merge_counts(
                    llm_tool_counts,
                    stage_bridge.get("llm_tool_call_counts"),
                )
    return tool_counts, llm_tool_counts


def _extract_run_metrics(run: dict) -> dict:
    usage = _coerce_usage(run)
    prompt_tokens = _int_value(usage.get("prompt_tokens"))
    completion_tokens = _int_value(usage.get("completion_tokens"))
    total_tokens = _int_value(usage.get("total_tokens")) or (
        prompt_tokens + completion_tokens
    )
    stages = {
        stage: seconds
        for stage in ("build", "scope", "query", "mediator")
        if (seconds := _stage_seconds(run, stage)) is not None
    }
    tool_counts, llm_tool_counts = _extract_bridge_counts(usage)
    return {
        "run_status": run.get("status") or "",
        "billing_status": run.get("billing_status") or "",
        "billing_cost_micro_usd": _int_value(run.get("billing_cost_micro_usd")),
        "llm_calls": _int_value(usage.get("calls") or usage.get("llm_calls")),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "duration_seconds": _run_seconds(run),
        "stage_seconds": stages,
        "tool_call_counts": tool_counts,
        "llm_tool_call_counts": llm_tool_counts,
        "telemetry_artifact_count": len(run.get("artifacts") or []),
    }


def _artifact_name(artifact: object) -> str:
    if isinstance(artifact, dict):
        for key in ("filename", "path", "url"):
            value = artifact.get(key)
            if value:
                return str(value)
    return str(artifact or "")


def _artifact_extensions(artifacts: list[object]) -> set[str]:
    extensions: set[str] = set()
    for artifact in artifacts:
        name = _artifact_name(artifact).split("?", 1)[0].rstrip("/")
        suffix = Path(name).suffix.lower()
        if suffix:
            extensions.add(suffix)
    return extensions


def _artifact_findings(
    artifacts: list[object],
    scenario: Scenario,
) -> list[GradeFinding]:
    if not scenario.required_artifact_extensions:
        return []
    found = _artifact_extensions(artifacts)
    findings: list[GradeFinding] = []
    for ext in scenario.required_artifact_extensions:
        expected = ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        if expected not in found:
            findings.append(
                GradeFinding(
                    kind="required_artifact_missing",
                    pattern=expected,
                    message=(
                        f"Run did not expose a required {expected} artifact."
                    ),
                    matched_text=",".join(sorted(found)),
                    dimension="artifact",
                    severity="fail",
                )
            )
    return findings


def _redact_process_text(text: str) -> str:
    redacted = text
    for pattern in (
        r"hmk_[A-Za-z0-9_-]+",
        r"phak_[A-Za-z0-9_-]+",
        r"Bearer\s+[A-Za-z0-9._~+/=-]+",
    ):
        redacted = re.sub(pattern, "<redacted>", redacted)
    return redacted.strip()[-2000:]


def _command_failure_findings(
    *,
    phase: str,
    returncode: int,
    stderr: str,
) -> list[GradeFinding]:
    if returncode == 0:
        return []
    excerpt = _redact_process_text(stderr)
    return [
        GradeFinding(
            kind=f"{phase}_command_failed",
            pattern=f"returncode={returncode}",
            message=f"hmctl {phase.replace('_', ' ')} command failed.",
            matched_text=excerpt,
            dimension="system",
            severity="fail",
        )
    ]


def _latency_findings(metrics: dict, scenario: Scenario) -> list[GradeFinding]:
    findings: list[GradeFinding] = []
    if scenario.max_duration_seconds is not None:
        actual = metrics.get("duration_seconds")
        if actual is None:
            findings.append(
                GradeFinding(
                    kind="latency_missing",
                    pattern="duration_seconds",
                    message="Run telemetry did not include total duration.",
                    dimension="performance",
                    severity="fail",
                )
            )
        elif float(actual) > scenario.max_duration_seconds:
            findings.append(
                GradeFinding(
                    kind="latency_over_budget",
                    pattern=f"duration_seconds<={scenario.max_duration_seconds:g}",
                    message="Run exceeded the scenario total latency budget.",
                    matched_text=str(actual),
                    dimension="performance",
                    severity="fail",
                )
            )
    stages = metrics.get("stage_seconds") or {}
    if not isinstance(stages, dict):
        stages = {}
    for stage, budget in scenario.max_stage_seconds.items():
        actual = stages.get(stage)
        if actual is None:
            findings.append(
                GradeFinding(
                    kind="latency_missing",
                    pattern=f"stage_seconds.{stage}",
                    message=f"Run telemetry did not include {stage} duration.",
                    dimension="performance",
                    severity="fail",
                )
            )
        elif float(actual) > budget:
            findings.append(
                GradeFinding(
                    kind="latency_over_budget",
                    pattern=f"stage_seconds.{stage}<={budget:g}",
                    message=f"{stage} stage exceeded the scenario latency budget.",
                    matched_text=str(actual),
                    dimension="performance",
                    severity="fail",
                )
            )
    return findings


def _rubric_from_findings(findings: list[GradeFinding]) -> list[RubricCheck]:
    return [
        RubricCheck(
            dimension=finding.dimension,
            severity=finding.severity,
            score=finding.score,
            passed=False,
            kind=finding.kind,
            message=finding.message,
            evidence=finding.matched_text,
            pattern=finding.pattern,
        )
        for finding in findings
    ]


def _artifact_rubric(
    artifacts: list[object],
    scenario: Scenario,
    findings: list[GradeFinding],
) -> list[RubricCheck]:
    if not scenario.required_artifact_extensions:
        return []
    if findings:
        return _rubric_from_findings(findings)
    return [
        RubricCheck(
            dimension="artifact",
            severity="pass",
            score=4,
            passed=True,
            kind="required_artifacts",
            message="Run exposed all required artifact extensions.",
            evidence=",".join(sorted(_artifact_extensions(artifacts))),
            pattern=";".join(scenario.required_artifact_extensions),
        )
    ]


def _latency_rubric(
    metrics: dict,
    scenario: Scenario,
    findings: list[GradeFinding],
) -> list[RubricCheck]:
    has_budget = (
        scenario.max_duration_seconds is not None
        or bool(scenario.max_stage_seconds)
    )
    if not has_budget:
        return []
    if findings:
        return _rubric_from_findings(findings)
    stage_seconds = metrics.get("stage_seconds")
    stage_evidence = json.dumps(stage_seconds, sort_keys=True) if stage_seconds else "{}"
    return [
        RubricCheck(
            dimension="performance",
            severity="pass",
            score=4,
            passed=True,
            kind="latency_budget",
            message="Run stayed within scenario latency budgets.",
            evidence=(
                f"duration_seconds={metrics.get('duration_seconds')}; "
                f"stage_seconds={stage_evidence}"
            ),
            pattern=(
                f"duration_seconds<={scenario.max_duration_seconds}; "
                f"stage_seconds={scenario.max_stage_seconds}"
            ),
        )
    ]


def _cmd_run_room(args: argparse.Namespace) -> int:
    scenario = SCENARIOS[args.scenario]
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    exit_code = 0

    for model in args.model:
        safe_model = model.replace("/", "_").replace(":", "_")
        run_path = out_dir / f"{scenario.id}__{safe_model}.json"
        cmd = [
            *_hmctl_base(args.hmctl_profile),
            "--yes",
            "--allow-degraded-attestation",
            "room",
            "ask",
            args.room,
            scenario.query,
            "--provider",
            args.provider,
            "--scope-model",
            model,
            "--query-model",
            model,
            "--mediator-model",
            model,
            "--max-tokens",
            str(args.max_tokens),
            "--max-llm-calls",
            str(args.max_llm_calls),
            "--timeout",
            str(args.timeout),
            "--json",
        ]
        if args.fetch:
            cmd.append("--fetch")
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=args.timeout + 120,
        )
        run_path.write_text(proc.stdout or "", encoding="utf-8")
        stderr_path = run_path.with_suffix(".stderr.txt")
        stderr_path.write_text(proc.stderr or "", encoding="utf-8")
        command_findings = _command_failure_findings(
            phase="room_ask",
            returncode=proc.returncode,
            stderr=proc.stderr or "",
        )

        output = ""
        run_id = ""
        artifacts = []
        telemetry = {}
        telemetry_path = None
        telemetry_stderr_path = None
        if proc.stdout.strip():
            try:
                data = json.loads(proc.stdout)
                output = data.get("output") or ""
                run_id = data.get("run_id") or ""
                artifacts = data.get("artifacts") or []
            except json.JSONDecodeError:
                exit_code = 1
        if run_id:
            telemetry_path = out_dir / f"{scenario.id}__{safe_model}__run.json"
            telemetry_stderr_path = telemetry_path.with_suffix(".stderr.txt")
            telemetry_proc = _fetch_hmroom_run_telemetry(
                args.room,
                run_id,
                args.hmctl_profile,
            )
            if telemetry_proc is None:
                telemetry_cmd = [
                    *_hmctl_base(args.hmctl_profile),
                    "--yes",
                    "--allow-degraded-attestation",
                    "room",
                    "runs",
                    run_id,
                    "--json",
                ]
                telemetry_proc = subprocess.run(
                    telemetry_cmd,
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
            command_findings.extend(
                _command_failure_findings(
                    phase="run_telemetry",
                    returncode=telemetry_proc.returncode,
                    stderr=telemetry_proc.stderr or "",
                )
            )
            telemetry_path.write_text(
                telemetry_proc.stdout or "", encoding="utf-8"
            )
            telemetry_stderr_path.write_text(
                telemetry_proc.stderr or "", encoding="utf-8"
            )
            if telemetry_proc.stdout.strip():
                try:
                    parsed = json.loads(telemetry_proc.stdout)
                    if isinstance(parsed, dict):
                        telemetry = parsed
                        artifacts = telemetry.get("artifacts") or artifacts
                except json.JSONDecodeError:
                    exit_code = 1
            if telemetry_proc.returncode != 0:
                exit_code = 1
        metrics = _extract_run_metrics(telemetry)
        text_grade = grade_text(output, scenario)
        artifact_findings = _artifact_findings(artifacts, scenario)
        latency_findings = _latency_findings(metrics, scenario)
        findings = (
            command_findings
            or [*text_grade.findings, *artifact_findings, *latency_findings]
        )
        rubric = (
            _rubric_from_findings(command_findings)
            if command_findings
            else [
                *text_grade.rubric,
                *_artifact_rubric(artifacts, scenario, artifact_findings),
                *_latency_rubric(metrics, scenario, latency_findings),
            ]
        )
        passed = not findings
        row = {
            "scenario": scenario.id,
            "model": model,
            "returncode": proc.returncode,
            "run_id": run_id,
            "passed": passed,
            "findings": [asdict(finding) for finding in findings],
            "rubric": [asdict(check) for check in rubric],
            "output_chars": len(output),
            "output_words": count_words(output),
            "markdown_table_count": count_markdown_tables(output),
            "artifact_count": len(artifacts),
            "artifact_filenames": [_artifact_name(a) for a in artifacts],
            "stdout_path": str(run_path),
            "stderr_path": str(stderr_path),
            "telemetry_path": str(telemetry_path) if telemetry_path else "",
            "telemetry_stderr_path": (
                str(telemetry_stderr_path) if telemetry_stderr_path else ""
            ),
            **metrics,
        }
        rows.append(row)
        print(json.dumps(row, sort_keys=True))
        if proc.returncode != 0 or not passed:
            exit_code = 1

    summary_path = out_dir / f"{scenario.id}__summary.json"
    summary_path.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval",
        description="Deterministic eval utilities for hivemind room agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List available deterministic scenarios.")

    grade = sub.add_parser("grade", help="Grade an output file or stdin.")
    grade.add_argument("scenario", choices=sorted(SCENARIOS))
    grade.add_argument(
        "output",
        help="Path to output text, or '-' to read from stdin.",
    )

    run_room = sub.add_parser(
        "run-room",
        help="Run one scenario against a room across one or more models.",
    )
    run_room.add_argument("scenario", choices=sorted(SCENARIOS))
    run_room.add_argument("room", help="Room id or invite accepted by hmctl room ask.")
    run_room.add_argument(
        "--model",
        action="append",
        required=True,
        help="Model id to test. Repeat for a model matrix.",
    )
    run_room.add_argument("--provider", default="openrouter")
    run_room.add_argument("--max-tokens", type=int, default=1_000_000)
    run_room.add_argument("--max-llm-calls", type=int, default=60)
    run_room.add_argument("--timeout", type=int, default=900)
    run_room.add_argument("--fetch", action="store_true")
    run_room.add_argument(
        "--hmctl-profile",
        help="Optional hmctl profile to pass before room commands.",
    )
    run_room.add_argument(
        "--output-dir",
        default="eval/results/live",
        help="Directory for raw hmctl JSON, stderr, and summary.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "list":
        return _cmd_list()
    if args.command == "grade":
        return _cmd_grade(args)
    if args.command == "run-room":
        return _cmd_run_room(args)
    parser.error(f"unknown command: {args.command}")
    return 2
