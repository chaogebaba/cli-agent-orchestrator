"""AC3.11 — CI lane cannot read the local tcache lane.

Parses .github/workflows/test-ci.yml and asserts it contains no reference to
tcache, TCACHE, /data/cao-scratch, or scripts/run-pytest.sh. This mechanically
enforces the no-cross-reads invariant (AC3.10) from the CI side, and also
enforces the CI entry-path claim (gate S6: CI invokes pytest directly, never
through the wrapper).

If the workflow ever routes through the local cache or wrapper, this test goes
red before the design invariant is violated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "test-ci.yml"

# Forbidden tokens: any reference to the local-lane cache or wrapper.
_FORBIDDEN_TOKENS = (
    "tcache",
    "TCACHE",
    "/data/cao-scratch",
    "scripts/run-pytest.sh",
)


@pytest.mark.unit
class TestCILaneIsolation:
    """AC3.11: the CI workflow must not reference the local tcache lane."""

    def test_workflow_exists(self) -> None:
        assert _WORKFLOW_PATH.exists(), f"CI workflow not found at {_WORKFLOW_PATH}"

    def test_no_tcache_references(self) -> None:
        """The CI workflow must contain zero references to tcache or the wrapper."""
        content = _WORKFLOW_PATH.read_text()
        violations: list[str] = []
        for token in _FORBIDDEN_TOKENS:
            if token in content:
                # Find line numbers for the report.
                for i, line in enumerate(content.splitlines(), 1):
                    if token in line:
                        violations.append(f"  line {i}: {token!r} in: {line.strip()}")
        assert not violations, (
            "CI workflow references the local tcache lane (AC3.10 violation):\n"
            + "\n".join(violations)
        )

    def test_no_deselect_lines(self) -> None:
        """AC2.3b: the workflow must have zero --deselect entries."""
        content = _WORKFLOW_PATH.read_text()
        deselect_count = content.count("--deselect")
        assert deselect_count == 0, (
            f"CI workflow still has {deselect_count} --deselect entries"
        )
