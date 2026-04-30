"""Score aggregation, terminal output, and JSON export."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from autoresearch.legacy_bench.judge import compute_scores, Judgment


# ANSI color codes
_COLORS = {
    "A": "\033[92m",  # green
    "B": "\033[96m",  # cyan
    "C": "\033[93m",  # yellow
    "D": "\033[91m",  # red
    "F": "\033[31m",  # dark red
}
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"


def _colored_grade(grade: str) -> str:
    color = _COLORS.get(grade, "")
    return f"{_BOLD}{color}{grade}{_RESET}"


def print_scenario_report(result: dict) -> None:
    """Print a single scenario's results to terminal."""
    scores = result["scores"]
    print(f"\n  {_BOLD}{result['scenario']}{_RESET}")
    print(f"  Policy: {_DIM}{result['policy'][:80]}{_RESET}")
    print(f"  Defense: {scores['defense_rate']:.0%} | "
          f"Utility: {scores['utility_score']:.0%} | "
          f"Combined: {scores['combined']:.0%} | "
          f"Grade: {_colored_grade(scores['grade'])}")

    # Per-round breakdown
    for rd in result.get("rounds", []):
        rs = rd["scores"]
        print(f"    Round {rd['round']}: "
              f"def={rs['defense_rate']:.0%} "
              f"util={rs['utility_score']:.0%} "
              f"grade={_colored_grade(rs['grade'])} "
              f"({rd['attack_count']} attacks)")

    # Worst leaks
    leaks = []
    for rd in result.get("rounds", []):
        for r in rd.get("results", []):
            j = r["judgment"]
            if j["leaked"]:
                leaks.append((j["severity"], r["attack"], j["evidence"]))

    if leaks:
        # Sort by severity
        severity_order = {"high": 0, "medium": 1, "low": 2, "none": 3}
        leaks.sort(key=lambda x: severity_order.get(x[0], 3))
        print(f"    Worst leaks ({len(leaks)} total):")
        for sev, attack, evidence in leaks[:3]:
            print(f"      [{sev}] {attack[:60]}...")
            print(f"        {_DIM}{evidence[:100]}{_RESET}")


def print_summary(results: list[dict]) -> None:
    """Print overall summary table."""
    print(f"\n{'═' * 60}")
    print(f"  {_BOLD}GAN ADVERSARIAL BENCHMARK SUMMARY{_RESET}")
    print(f"{'═' * 60}")

    # Collect all judgments for overall score
    all_judgments = []
    for result in results:
        for rd in result.get("rounds", []):
            for r in rd.get("results", []):
                j = r["judgment"]
                all_judgments.append(Judgment(
                    leaked=j["leaked"],
                    severity=j["severity"],
                    evidence=j["evidence"],
                    structural_flags=j.get("structural_flags", []),
                    useful=j["useful"],
                ))

    overall = compute_scores(all_judgments)

    print(f"\n  {'Scenario':<25} {'Defense':>8} {'Utility':>8} {'Combined':>9} {'Grade':>6}")
    print(f"  {'─' * 25} {'─' * 8} {'─' * 8} {'─' * 9} {'─' * 6}")

    for result in results:
        s = result["scores"]
        print(f"  {result['scenario']:<25} "
              f"{s['defense_rate']:>7.0%} "
              f"{s['utility_score']:>7.0%} "
              f"{s['combined']:>8.0%} "
              f"   {_colored_grade(s['grade'])}")

    print(f"  {'─' * 25} {'─' * 8} {'─' * 8} {'─' * 9} {'─' * 6}")
    print(f"  {'OVERALL':<25} "
          f"{overall['defense_rate']:>7.0%} "
          f"{overall['utility_score']:>7.0%} "
          f"{overall['combined']:>8.0%} "
          f"   {_colored_grade(overall['grade'])}")

    total_elapsed = sum(r.get("elapsed_ms", 0) for r in results)
    total_attacks = sum(
        sum(rd["attack_count"] for rd in r.get("rounds", []))
        for r in results
    )
    print(f"\n  Total attacks: {total_attacks} | "
          f"Time: {total_elapsed / 1000:.1f}s | "
          f"Defended: {overall['defended']}/{overall['total']}")
    print()


def export_json(results: list[dict], output_dir: str = "autoresearch/legacy_bench/results") -> str:
    """Export results to a timestamped JSON file. Returns the file path."""
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    filename = f"gan-{timestamp}.json"
    filepath = os.path.join(output_dir, filename)

    # Compute overall scores
    all_judgments = []
    for result in results:
        for rd in result.get("rounds", []):
            for r in rd.get("results", []):
                j = r["judgment"]
                all_judgments.append(Judgment(
                    leaked=j["leaked"],
                    severity=j["severity"],
                    evidence=j["evidence"],
                    structural_flags=j.get("structural_flags", []),
                    useful=j["useful"],
                ))

    overall = compute_scores(all_judgments)

    export = {
        "timestamp": timestamp,
        "overall_scores": overall,
        "scenarios": results,
    }

    with open(filepath, "w") as f:
        json.dump(export, f, indent=2, default=str)

    # Also write a "latest" symlink
    latest_path = os.path.join(output_dir, "gan-latest.json")
    with open(latest_path, "w") as f:
        json.dump(export, f, indent=2, default=str)

    return filepath


def load_report(filepath: str) -> dict:
    """Load a JSON report file."""
    with open(filepath) as f:
        return json.load(f)


def print_report_from_file(filepath: str) -> None:
    """Load and print a report from a JSON file."""
    data = load_report(filepath)
    for result in data.get("scenarios", []):
        print_scenario_report(result)
    print_summary(data.get("scenarios", []))
