from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scenario:
    """A deterministic eval case for a room-agent stack.

    The harness grades observable outputs here. Full room runs should attach
    additional telemetry such as stage latency, token usage, tool calls, and
    room manifest hashes beside these scenario-level checks.
    """

    id: str
    title: str
    query: str
    policy: str
    required_patterns: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    min_output_words: int = 0
    min_markdown_tables: int = 0
    required_artifact_extensions: tuple[str, ...] = ()
    max_duration_seconds: float | None = None
    max_stage_seconds: dict[str, float] = field(default_factory=dict)
    superpower_demand: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class GradeFinding:
    kind: str
    pattern: str
    message: str
    matched_text: str = ""


@dataclass(frozen=True)
class GradeResult:
    scenario_id: str
    passed: bool
    findings: list[GradeFinding] = field(default_factory=list)
