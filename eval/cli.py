from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from .graders import grade_text
from .scenarios import SCENARIOS


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


def _hmctl_base() -> list[str]:
    return shlex.split(os.environ.get("HMCTL_BIN", "uv run hmctl"))


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
            *_hmctl_base(),
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

        output = ""
        run_id = ""
        artifacts = []
        if proc.stdout.strip():
            try:
                data = json.loads(proc.stdout)
                output = data.get("output") or ""
                run_id = data.get("run_id") or ""
                artifacts = data.get("artifacts") or []
            except json.JSONDecodeError:
                exit_code = 1
        grade = grade_text(output, scenario)
        row = {
            "scenario": scenario.id,
            "model": model,
            "returncode": proc.returncode,
            "run_id": run_id,
            "passed": grade.passed,
            "findings": [asdict(finding) for finding in grade.findings],
            "output_chars": len(output),
            "artifact_count": len(artifacts),
            "stdout_path": str(run_path),
            "stderr_path": str(stderr_path),
        }
        rows.append(row)
        print(json.dumps(row, sort_keys=True))
        if proc.returncode != 0 or not grade.passed:
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
