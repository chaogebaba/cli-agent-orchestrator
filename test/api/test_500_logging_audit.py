"""C3 audit: Assert zero bare HTTPException(500) without preceding logger.exception.

This test greps the api/main.py source to ensure every 500-class raise has
a logger.exception (or logger.error) call within the 3 lines preceding it.
"""

import re
from pathlib import Path

import pytest

API_MAIN = (
    Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator" / "api" / "main.py"
)

# Patterns that match a 500 raise
RAISE_500_PATTERNS = [
    re.compile(r"raise HTTPException\(status_code=500"),
    re.compile(r"raise HTTPException\(status_code=status\.HTTP_500_INTERNAL_SERVER_ERROR"),
]

# Patterns that count as logging before the raise
LOG_PATTERNS = [
    re.compile(r"logger\.exception\("),
    re.compile(r"logger\.error\("),
]


def test_all_500_raises_have_preceding_log():
    """Every 500 HTTPException raise must be preceded by a logger call."""
    source = API_MAIN.read_text()
    lines = source.splitlines()

    violations = []
    for i, line in enumerate(lines):
        is_500_raise = any(pat.search(line) for pat in RAISE_500_PATTERNS)
        if not is_500_raise:
            continue

        # Check preceding 3 lines for a log call
        window = lines[max(0, i - 3) : i]
        has_log = any(any(log_pat.search(wl) for log_pat in LOG_PATTERNS) for wl in window)
        if not has_log:
            violations.append(f"  line {i + 1}: {line.strip()}")

    assert not violations, (
        f"Found {len(violations)} bare 500 raise(s) without preceding "
        f"logger.exception/logger.error:\n" + "\n".join(violations)
    )
