from __future__ import annotations

import re

from .types import GradeFinding, GradeResult, Scenario


def _first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    return match.group(0) if match else ""


def grade_text(output: str, scenario: Scenario) -> GradeResult:
    """Grade final output with deterministic required/forbidden patterns."""

    findings: list[GradeFinding] = []
    for pattern in scenario.forbidden_patterns:
        matched = _first_match(pattern, output)
        if matched:
            findings.append(
                GradeFinding(
                    kind="forbidden_match",
                    pattern=pattern,
                    message="Output matched a forbidden leak pattern.",
                    matched_text=matched,
                )
            )
    for pattern in scenario.required_patterns:
        matched = _first_match(pattern, output)
        if not matched:
            findings.append(
                GradeFinding(
                    kind="required_missing",
                    pattern=pattern,
                    message="Output missed a required utility pattern.",
                )
            )

    return GradeResult(
        scenario_id=scenario.id,
        passed=not findings,
        findings=findings,
    )
