"""Room-native evaluation harness.

The active eval package is intentionally separate from the retired
autoresearch benchmark archive. It should measure current room agents with
deterministic graders, stage-level latency, and scenario tags that force
adaptive scope behavior.
"""

from .graders import grade_text
from .scenarios import SCENARIOS
from .types import GradeFinding, GradeResult, Scenario

__all__ = [
    "GradeFinding",
    "GradeResult",
    "SCENARIOS",
    "Scenario",
    "grade_text",
]
