from __future__ import annotations

import argparse
import json
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "list":
        return _cmd_list()
    if args.command == "grade":
        return _cmd_grade(args)
    parser.error(f"unknown command: {args.command}")
    return 2
