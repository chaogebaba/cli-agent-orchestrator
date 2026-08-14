"""Test for F188: sandbox up creates parent directories (mkdir -p semantics)."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest


class TestF188MissingParentDir:
    """sandbox up must create missing parent directories (mkdir -p semantics)."""

    def test_disk_usage_on_missing_parent_raises(self, tmp_path: Path) -> None:
        """Pre-fix behavior: shutil.disk_usage on non-existent parent raises."""
        nonexistent = tmp_path / "deep" / "nested" / "sbx"
        assert not nonexistent.parent.exists()
        with pytest.raises(FileNotFoundError):
            shutil.disk_usage(str(nonexistent.parent))

    def test_command_up_creates_parent_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Post-fix: command_up creates parent directories before disk_usage."""
        from cli_agent_orchestrator.sandbox_bootstrap import command_up

        root = tmp_path / "deep" / "nested" / "sbx"
        assert not root.parent.exists()

        args = argparse.Namespace(root=str(root), port=9890, fixture_mock_cli_variant=None)

        # command_up will fail later (no real venv etc.) but parent must exist
        # after the parent-creation logic. Patch _build_manifest to stop early.
        with patch(
            "cli_agent_orchestrator.sandbox_bootstrap._build_manifest",
            side_effect=RuntimeError("stop-after-parent-creation"),
        ):
            with pytest.raises(RuntimeError, match="stop-after-parent-creation"):
                command_up(args)

        # The fix creates the parent directory
        assert root.parent.exists()
