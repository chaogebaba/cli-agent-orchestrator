"""F332 AC10: Server-internal inbox writers never POST to /inbox/messages over HTTP.

This structural assertion ensures that no module under services/ uses HTTP to
write inbox messages. Internal writers construct InboxModel directly — they
never touch the authenticated endpoint and therefore need no token.
"""

import ast
import os
from pathlib import Path

import pytest


SERVICES_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cli_agent_orchestrator"
    / "services"
)


def _python_files_in_services():
    """Yield all .py files under the services directory."""
    for f in sorted(SERVICES_DIR.glob("**/*.py")):
        if f.name.startswith("__"):
            continue
        yield f


@pytest.mark.parametrize(
    "source_file",
    list(_python_files_in_services()),
    ids=[f.name for f in _python_files_in_services()],
)
def test_no_service_posts_to_inbox_endpoint(source_file: Path):
    """No service module should HTTP-POST to the inbox messages endpoint.

    Internal inbox writers (watchdog, identity-authority, orphan notices, WPM1,
    barrier fire, deferred-init-failure) all construct InboxModel directly.
    If a service starts POSTing to /inbox/messages, it would need a token —
    and issuing one to an internal writer creates a forger where none existed (D10).
    """
    content = source_file.read_text(encoding="utf-8")

    # Check for patterns that indicate HTTP calls to the inbox endpoint
    suspicious_patterns = [
        "/inbox/messages",
        "inbox/messages",
    ]

    for pattern in suspicious_patterns:
        if pattern in content:
            # Allow the pattern in comments/docstrings only — not in string
            # literals that look like URL paths
            lines_with_pattern = [
                (i + 1, line.strip())
                for i, line in enumerate(content.splitlines())
                if pattern in line
                and not line.strip().startswith("#")
                and not line.strip().startswith('"""')
                and not line.strip().startswith("'''")
                # Allow test references and type annotations
                and "def " not in line
                and "class " not in line
            ]
            # Filter out lines that are clearly in docstrings or comments
            real_hits = [
                (lineno, line)
                for lineno, line in lines_with_pattern
                if "post(" in line.lower()
                or "requests." in line.lower()
                or "cao_http" in line.lower()
                or "httpx" in line.lower()
            ]
            assert not real_hits, (
                f"Service {source_file.name} appears to HTTP-POST to the inbox endpoint "
                f"(D10 violation). Internal writers must construct InboxModel directly.\n"
                f"Hits: {real_hits}"
            )
