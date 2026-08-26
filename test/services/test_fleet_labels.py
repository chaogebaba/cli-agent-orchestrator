"""F483: Tests for fleet_labels.py — TSV write/remove logic."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.fleet_labels import (
    FLEET_LABELS_PATH,
    MAX_LABEL_LENGTH,
    _atomic_remove,
    _atomic_upsert,
    _read_lines,
    _sanitize_label,
    _write_atomic,
    remove_label,
    upsert_label,
)


@pytest.fixture
def tmp_tsv(tmp_path):
    """Provide a temporary TSV path and patch FLEET_LABELS_PATH."""
    tsv = tmp_path / "fleet-labels.tsv"
    with patch("cli_agent_orchestrator.services.fleet_labels.FLEET_LABELS_PATH", tsv):
        yield tsv


class TestSanitizeLabel:
    def test_strips_tabs(self):
        assert _sanitize_label("hello\tworld") == "hello world"

    def test_strips_newlines(self):
        assert _sanitize_label("hello\nworld") == "hello world"

    def test_strips_carriage_returns(self):
        assert _sanitize_label("hello\rworld") == "helloworld"

    def test_truncates_to_max_length(self):
        long_label = "a" * 60
        assert len(_sanitize_label(long_label)) == MAX_LABEL_LENGTH

    def test_passes_normal_label(self):
        assert _sanitize_label("F483 build") == "F483 build"


class TestUpsertLabel:
    def test_creates_file_and_writes_row(self, tmp_tsv):
        upsert_label("abc12345", "test task")
        content = tmp_tsv.read_text()
        assert content == "abc12345\ttest task\n"

    def test_appends_new_row(self, tmp_tsv):
        tmp_tsv.write_text("existing1\told label\n")
        upsert_label("newtermid", "new task")
        lines = tmp_tsv.read_text().splitlines()
        assert len(lines) == 2
        assert lines[0] == "existing1\told label"
        assert lines[1] == "newtermid\tnew task"

    def test_updates_existing_row(self, tmp_tsv):
        tmp_tsv.write_text("abc12345\told label\nother123\tkeep\n")
        upsert_label("abc12345", "updated label")
        lines = tmp_tsv.read_text().splitlines()
        assert len(lines) == 2
        assert lines[0] == "abc12345\tupdated label"
        assert lines[1] == "other123\tkeep"

    def test_empty_label_noop(self, tmp_tsv):
        upsert_label("abc12345", "")
        assert not tmp_tsv.exists()

    def test_label_truncated_to_max(self, tmp_tsv):
        upsert_label("abc12345", "x" * 60)
        content = tmp_tsv.read_text()
        tid, label = content.strip().split("\t", 1)
        assert len(label) == MAX_LABEL_LENGTH

    def test_never_raises_on_unwritable_dir(self, tmp_path):
        bad_path = tmp_path / "nonexistent" / "deep" / "path" / "fleet-labels.tsv"
        # Make parent unwritable (but it won't exist)
        with patch("cli_agent_orchestrator.services.fleet_labels.FLEET_LABELS_PATH", bad_path):
            # Should not raise — the parent dirs should be created
            upsert_label("abc12345", "test")
            assert bad_path.exists()

    def test_never_raises_on_permission_error(self, tmp_tsv, tmp_path):
        # Make the directory read-only
        ro_dir = tmp_path / "ro"
        ro_dir.mkdir()
        ro_tsv = ro_dir / "fleet-labels.tsv"
        os.chmod(ro_dir, 0o444)
        with patch("cli_agent_orchestrator.services.fleet_labels.FLEET_LABELS_PATH", ro_tsv):
            # Should not raise
            upsert_label("abc12345", "test")
        os.chmod(ro_dir, 0o755)  # restore for cleanup


class TestRemoveLabel:
    def test_removes_matching_row(self, tmp_tsv):
        tmp_tsv.write_text("abc12345\ttask A\nother123\ttask B\n")
        remove_label("abc12345")
        lines = tmp_tsv.read_text().splitlines()
        assert len(lines) == 1
        assert lines[0] == "other123\ttask B"

    def test_noop_when_id_not_found(self, tmp_tsv):
        tmp_tsv.write_text("abc12345\ttask A\n")
        remove_label("notfound")
        content = tmp_tsv.read_text()
        assert content == "abc12345\ttask A\n"

    def test_noop_when_file_missing(self, tmp_tsv):
        # Should not raise
        remove_label("abc12345")

    def test_never_raises_on_permission_error(self, tmp_path):
        ro_dir = tmp_path / "ro"
        ro_dir.mkdir()
        ro_tsv = ro_dir / "fleet-labels.tsv"
        ro_tsv.write_text("abc12345\ttask A\n")
        os.chmod(ro_dir, 0o444)
        with patch("cli_agent_orchestrator.services.fleet_labels.FLEET_LABELS_PATH", ro_tsv):
            # Should not raise
            remove_label("abc12345")
        os.chmod(ro_dir, 0o755)  # restore for cleanup


class TestAtomicOperations:
    """Test the internal atomic read-modify-write helpers."""

    def test_read_lines_missing_file(self, tmp_path):
        result = _read_lines(tmp_path / "nonexistent.tsv")
        assert result == []

    def test_write_atomic_creates_file(self, tmp_path):
        tsv = tmp_path / "test.tsv"
        _write_atomic(tsv, ["line1\n", "line2\n"])
        assert tsv.read_text() == "line1\nline2\n"

    def test_write_atomic_replaces_existing(self, tmp_path):
        tsv = tmp_path / "test.tsv"
        tsv.write_text("old content\n")
        _write_atomic(tsv, ["new content\n"])
        assert tsv.read_text() == "new content\n"


class TestTsvFormatContract:
    """Verify the HARD CONTRACT: rows are exactly 2-field <terminal_id>\\t<label>."""

    def test_format_matches_fleet_tui_read_labels(self, tmp_tsv):
        """Simulate fleet-tui.py's read_labels() parsing."""
        upsert_label("abc12345", "my task label")
        upsert_label("def67890", "another task")

        # Simulate fleet-tui.py read_labels()
        labels = {}
        with open(tmp_tsv) as f:
            for line in f:
                if "\t" in line:
                    tid, label = line.rstrip("\n").split("\t", 1)
                    labels[tid.strip()] = label.strip()

        assert labels == {
            "abc12345": "my task label",
            "def67890": "another task",
        }

    def test_no_extra_columns(self, tmp_tsv):
        """Ensure no timestamp or profile columns sneak in."""
        upsert_label("abc12345", "test")
        content = tmp_tsv.read_text().strip()
        parts = content.split("\t")
        assert len(parts) == 2, f"Expected exactly 2 fields, got {len(parts)}: {parts}"

    def test_label_with_spaces_preserved(self, tmp_tsv):
        upsert_label("abc12345", "F483 build worktree")
        content = tmp_tsv.read_text().strip()
        tid, label = content.split("\t", 1)
        assert tid == "abc12345"
        assert label == "F483 build worktree"


class TestEnvOverride:
    """Test CAO_FLEET_LABELS_PATH env variable override."""

    def test_respects_env_override(self, tmp_path, monkeypatch):
        custom_path = str(tmp_path / "custom-labels.tsv")
        monkeypatch.setenv("CAO_FLEET_LABELS_PATH", custom_path)
        # Re-import to pick up the env var
        import importlib

        import cli_agent_orchestrator.services.fleet_labels as fl_mod

        importlib.reload(fl_mod)
        assert str(fl_mod.FLEET_LABELS_PATH) == custom_path
        # Restore
        monkeypatch.delenv("CAO_FLEET_LABELS_PATH", raising=False)
        importlib.reload(fl_mod)
