"""Integration tests for profile_validator that require subprocess.

Separated from the unit-tier test_profile_validator.py to satisfy the G2
tier guard (no subprocess calls in unit-tier files).
"""

import json
import os
import subprocess
import sys

import pytest

from cli_agent_orchestrator.services.profile_validator import (
    _MAX_FINDINGS,
    _OMISSION_MESSAGE,
)

pytestmark = pytest.mark.integration


class TestAggregateFindingBudgetIntegration:
    """Tests requiring subprocess to verify cross-process determinism."""

    def test_schema_prefix_is_stable_across_hash_seeds(self) -> None:
        """Truncation must select the same document-order prefix in every worker."""
        script = """
import json
from cli_agent_orchestrator.services.profile_validator import validate_frontmatter

metadata = {
    "name": "agent",
    "mcpServers": {f"srv{index:04}": {} for index in range(299, -1, -1)},
}
findings = validate_frontmatter(metadata)
print(json.dumps([
    {"severity": finding.severity, "message": finding.message, "path": finding.path}
    for finding in findings
]))
"""
        outputs = []
        for seed in ("0", "1", "2", "3"):
            environment = os.environ.copy()
            environment["PYTHONHASHSEED"] = seed
            completed = subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            outputs.append(completed.stdout)

        assert len(set(outputs)) == 1
        findings = json.loads(outputs[0])
        selected_document_prefix = [
            f"mcpServers.srv{index:04}" for index in range(299, 299 - (_MAX_FINDINGS - 1), -1)
        ]
        assert [finding["path"] for finding in findings[:-1]] == sorted(selected_document_prefix)
        assert findings[-1] == {
            "severity": "error",
            "message": _OMISSION_MESSAGE,
            "path": None,
        }
