"""Tests for F137 AC5: installer preflight ordering.

Proves:
- Preflight runs before uv tool install, config reconcile, and profile installation.
- A failing preflight blocks all subsequent commands.
- The installer uses the candidate source tree, not the installed binary.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest


def _find_install_sh() -> Path:
    """Locate install.sh relative to the test file."""
    # test/ -> cli-agent-orchestrator/ -> cli-subagents/install.sh
    test_dir = Path(__file__).resolve().parent
    # Navigate up to cli-agent-orchestrator root, then to parent
    cao_root = test_dir.parent.parent.parent
    install_sh = cao_root.parent / "install.sh"
    if not install_sh.exists():
        pytest.skip(f"install.sh not found at {install_sh}")
    return install_sh


class TestInstallerPreflightOrdering:
    """AC5: failing source-tree preflight occurs before any mutation."""

    def test_install_sh_has_preflight_before_uv_tool_install(self):
        """Structural: preflight invocation appears before uv tool install."""
        install_sh = _find_install_sh()
        content = install_sh.read_text()
        preflight_pos = content.find("cao config preflight --activation")
        uv_install_pos = content.find("uv tool install --force")
        reconcile_pos = content.find("cao config reconcile")
        profile_pos = content.find("cao install")

        assert preflight_pos != -1, "preflight command not found in install.sh"
        assert uv_install_pos != -1, "uv tool install not found in install.sh"
        assert reconcile_pos != -1, "config reconcile not found in install.sh"
        assert profile_pos != -1, "cao install not found in install.sh"

        # Preflight MUST come before all mutations
        assert preflight_pos < uv_install_pos, "preflight must precede uv tool install"
        assert preflight_pos < reconcile_pos, "preflight must precede config reconcile"
        assert preflight_pos < profile_pos, "preflight must precede profile install"

    def test_install_sh_exits_on_preflight_failure(self):
        """Structural: install.sh exits immediately if preflight fails."""
        install_sh = _find_install_sh()
        content = install_sh.read_text()
        # The script must have `if ! ... preflight` that exits on failure
        assert "exit 1" in content
        # Verify the conditional pattern exists
        assert "if ! uv run" in content
        assert "cao config preflight --activation" in content

    def test_install_sh_uses_source_tree_not_installed_binary(self):
        """AC5: invocation uses `uv run --directory` against the fork root."""
        install_sh = _find_install_sh()
        content = install_sh.read_text()
        # Must use uv run --directory to invoke the SOURCE tree
        assert 'uv run --directory "$FORK_DIR"' in content
        # The FORK_DIR must be defined relative to REPO_DIR
        assert 'FORK_DIR="$REPO_DIR/cli-agent-orchestrator"' in content

    def test_install_sh_preflight_before_any_cao_command(self):
        """No `cao` command (which requires the installed binary) runs before preflight."""
        install_sh = _find_install_sh()
        lines = install_sh.read_text().splitlines()

        preflight_line = None
        for i, line in enumerate(lines):
            if "cao config preflight" in line:
                preflight_line = i
                break

        assert preflight_line is not None

        # Check no `cao` binary invocation (not via uv run) precedes the preflight
        for i in range(preflight_line):
            line = lines[i].strip()
            if line.startswith("#") or not line:
                continue
            # `uv run ... cao` is fine (source tree); bare `cao` is not
            if "cao " in line and "uv run" not in line:
                pytest.fail(
                    f"Line {i+1} invokes installed `cao` before preflight: {line}"
                )

    def test_install_sh_resolves_from_script_location(self):
        """The script resolves REPO_DIR from its own location, not $PWD."""
        install_sh = _find_install_sh()
        content = install_sh.read_text()
        assert 'REPO_DIR="$(cd "$(dirname "$0")" && pwd)"' in content


class TestInstallerFakeHarness:
    """Prove preflight ordering with a fake command harness (V4 arm 7)."""

    def test_failing_preflight_blocks_subsequent_commands(self, tmp_path):
        """A script that mimics install.sh ordering proves nothing runs after failure."""
        # Create a fake uv that records whether it was called
        marker = tmp_path / "uv_tool_called"
        fake_uv = tmp_path / "uv"
        fake_uv.write_text(
            f"""#!/bin/sh
if [ "$1" = "run" ]; then
    # Simulate preflight failure
    echo "FAIL — prefix drop-in found"
    exit 1
fi
if [ "$1" = "tool" ]; then
    touch "{marker}"
    exit 0
fi
"""
        )
        fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Create a minimal install.sh that uses our fake uv
        test_script = tmp_path / "test_install.sh"
        test_script.write_text(
            f"""#!/bin/sh
set -eu
PATH="{tmp_path}:$PATH"
FORK_DIR="{tmp_path}/fork"
if ! uv run --directory "$FORK_DIR" cao config preflight --activation; then
    echo "FATAL: preflight FAILED"
    exit 1
fi
uv tool install --force --python 3.14 "$FORK_DIR"
"""
        )
        test_script.chmod(test_script.stat().st_mode | stat.S_IXUSR)

        result = subprocess.run(
            [str(test_script)],
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert result.returncode == 1
        assert "FATAL" in result.stdout or "FATAL" in result.stderr or "FAIL" in result.stdout
        assert not marker.exists(), "uv tool install should NOT have been called"
