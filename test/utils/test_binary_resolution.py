"""Tests for resolve_provider_binary (C1)."""

import os
import stat
import tempfile

import pytest

from cli_agent_orchestrator.utils.binary_resolution import resolve_provider_binary


class TestResolveProviderBinary:
    """C1: Absolute binary resolution for provider spawn."""

    def test_returns_absolute_path_when_binary_in_path(self):
        """When the binary is findable via shutil.which, return absolute path."""
        # 'python3' (or 'python') is guaranteed present in test env
        result = resolve_provider_binary("python3")
        assert os.path.isabs(result)
        assert os.path.isfile(result)

    def test_returns_env_override_when_set(self, monkeypatch, tmp_path):
        """CAO_<NAME>_PATH env override takes effect when file exists."""
        fake_bin = tmp_path / "codex"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("CAO_CODEX_PATH", str(fake_bin))
        # Ensure shutil.which won't find it via PATH and no fallback dirs match
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setenv("HOME", "/nonexistent_home")
        result = resolve_provider_binary("codex")
        assert result == str(fake_bin)

    def test_returns_fallback_dir_path_when_only_there(self, monkeypatch, tmp_path):
        """Finds binary in fallback dirs when not in PATH and no env override."""
        # Create a fake ~/.local/bin/somebinary
        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True)
        fake_bin = local_bin / "somebinary"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setenv("HOME", str(tmp_path))
        # Remove any env override
        monkeypatch.delenv("CAO_SOMEBINARY_PATH", raising=False)
        result = resolve_provider_binary("somebinary")
        assert result == str(fake_bin)
        assert os.path.isabs(result)

    def test_returns_bare_name_when_not_found_anywhere(self, monkeypatch):
        """When binary is truly not found, return bare name as last resort."""
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setenv("HOME", "/nonexistent_home")
        monkeypatch.delenv("CAO_NOTREAL_PATH", raising=False)
        result = resolve_provider_binary("notreal")
        assert result == "notreal"

    def test_env_override_ignored_when_file_missing(self, monkeypatch):
        """CAO_<NAME>_PATH is ignored when the pointed-to file doesn't exist."""
        monkeypatch.setenv("CAO_GHOST_PATH", "/nonexistent/ghost")
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setenv("HOME", "/nonexistent_home")
        result = resolve_provider_binary("ghost")
        assert result == "ghost"

    def test_hyphenated_name_env_key(self, monkeypatch, tmp_path):
        """Hyphens in binary name are converted to underscores for env key."""
        fake_bin = tmp_path / "my-tool"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("CAO_MY_TOOL_PATH", str(fake_bin))
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setenv("HOME", "/nonexistent_home")
        result = resolve_provider_binary("my-tool")
        assert result == str(fake_bin)
