"""Additional agent_profiles tests for coverage gaps.

Covers: _scan_directory for .md files and subdirs with frontmatter errors,
load_agent_profile from provider/extra dirs, built-in fallback with missing fields,
and list_agent_profiles with frontmatter parse errors.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile


class TestScanDirectory:
    """Test _scan_directory for .md files and subdirectories."""

    def test_scan_md_files(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        # Create .md file with frontmatter
        md_file = tmp_path / "my-agent.md"
        md_file.write_text("---\ndescription: My agent\n---\nPrompt content")

        profiles = {}
        _scan_directory(tmp_path, "test", profiles)

        assert "my-agent" in profiles
        assert profiles["my-agent"]["description"] == "My agent"
        assert profiles["my-agent"]["source"] == "test"

    def test_scan_subdirectory_with_agent_md(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        # Create subdirectory with agent.md
        agent_dir = tmp_path / "sub-agent"
        agent_dir.mkdir()
        (agent_dir / "agent.md").write_text("---\ndescription: Sub agent\n---\nPrompt")

        profiles = {}
        _scan_directory(tmp_path, "test", profiles)

        assert "sub-agent" in profiles
        assert profiles["sub-agent"]["description"] == "Sub agent"

    def test_scan_subdirectory_without_agent_md(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        # A subdirectory WITHOUT agent.md is not a profile and must be skipped
        # entirely (F558 #413) — recording it previously produced a fake
        # profile whose load/charter-projection raised FileNotFoundError and
        # crashed the session brief on a fresh install. This is a structural
        # rule (no agent.md -> not a profile), not a name blacklist.
        #
        # Mutation note: revert the `continue`/`not agent_md.exists()` guard in
        # _scan_directory (record the bare dir as {loadable: False}) and this
        # assertion flips to a KeyError-free membership and fails.
        (tmp_path / "bare-agent").mkdir()

        profiles = {}
        _scan_directory(tmp_path, "test", profiles)

        assert "bare-agent" not in profiles
        assert profiles == {}

    def test_scan_md_file_with_bad_frontmatter(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        # Create .md file with invalid frontmatter
        md_file = tmp_path / "broken.md"
        md_file.write_text("not valid yaml frontmatter ::::")

        profiles = {}
        _scan_directory(tmp_path, "test", profiles)

        # Should still be added with empty description
        assert "broken" in profiles
        assert profiles["broken"]["description"] == ""

    def test_scan_subdirectory_with_bad_agent_md(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        agent_dir = tmp_path / "bad-sub"
        agent_dir.mkdir()
        (agent_dir / "agent.md").write_text("invalid: [yaml: broken")

        profiles = {}
        _scan_directory(tmp_path, "test", profiles)

        # Should still be added with empty description
        assert "bad-sub" in profiles
        assert profiles["bad-sub"]["description"] == ""

    def test_scan_nonexistent_directory(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        profiles = {}
        _scan_directory(tmp_path / "nonexistent", "test", profiles)

        assert profiles == {}

    def test_scan_deduplicates(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        (tmp_path / "agent.md").write_text("---\ndescription: First\n---\n")

        profiles = {"agent": {"name": "agent", "description": "Existing", "source": "other"}}
        _scan_directory(tmp_path, "test", profiles)

        # Should keep existing entry
        assert profiles["agent"]["source"] == "other"
        assert profiles["agent"]["description"] == "Existing"


class TestLoadAgentProfileFromProviderDirs:
    """Test load_agent_profile searching provider and extra directories.

    These tests exercise the real _read_agent_profile_source path so the
    _safe_join traversal guard is covered end-to-end. The local store is
    pointed at an empty tmp directory so lookups fall through to the
    provider / extra / built-in stores the test cares about.
    """

    @staticmethod
    def _empty_local(tmp_path, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
            tmp_path / "empty-local",
        )

    def test_load_from_provider_flat_file(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        self._empty_local(tmp_path, monkeypatch)
        provider_dir = tmp_path / "provider"
        provider_dir.mkdir()
        (provider_dir / "my-agent.md").write_text(
            "---\nname: my-agent\ndescription: Provider agent\n---\nPrompt"
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            lambda: {"kiro_cli": str(provider_dir)},
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )

        result = load_agent_profile("my-agent")

        assert result.name == "my-agent"
        assert result.description == "Provider agent"

    def test_load_from_provider_directory_style(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        self._empty_local(tmp_path, monkeypatch)
        provider_dir = tmp_path / "provider"
        (provider_dir / "my-agent").mkdir(parents=True)
        (provider_dir / "my-agent" / "agent.md").write_text(
            "---\ndescription: Dir agent\n---\nPrompt"
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            lambda: {"kiro_cli": str(provider_dir)},
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )

        result = load_agent_profile("my-agent")

        assert result.name == "my-agent"  # Filled in because missing from frontmatter
        assert result.description == "Dir agent"

    def test_load_from_extra_dirs_flat(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        self._empty_local(tmp_path, monkeypatch)
        extra = tmp_path / "extra"
        extra.mkdir()
        (extra / "custom-agent.md").write_text(
            "---\nname: custom-agent\ndescription: Custom\n---\nPrompt"
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
            lambda: [str(extra)],
        )

        result = load_agent_profile("custom-agent")

        assert result.name == "custom-agent"

    def test_load_from_extra_dirs_directory_style(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        self._empty_local(tmp_path, monkeypatch)
        extra = tmp_path / "extra"
        (extra / "dir-agent").mkdir(parents=True)
        (extra / "dir-agent" / "agent.md").write_text("---\ndescription: Dir style\n---\nPrompt")
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
            lambda: [str(extra)],
        )

        result = load_agent_profile("dir-agent")

        assert result.name == "dir-agent"

    def test_skips_nonexistent_provider_dir(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        self._empty_local(tmp_path, monkeypatch)
        empty_builtin = tmp_path / "builtin"
        empty_builtin.mkdir()
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.resources.files",
            lambda _pkg: empty_builtin,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            lambda: {"kiro_cli": str(tmp_path / "does-not-exist")},
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
            lambda: [str(tmp_path / "also-missing")],
        )

        with pytest.raises(FileNotFoundError, match="Agent profile not found"):
            load_agent_profile("missing-agent")

    def test_builtin_fills_missing_name_and_description(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        self._empty_local(tmp_path, monkeypatch)
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        (builtin / "bare-agent.md").write_text("---\n---\nJust a prompt")
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.resources.files",
            lambda _pkg: builtin,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )

        result = load_agent_profile("bare-agent")

        assert result.name == "bare-agent"
        assert result.description == ""

    def test_safe_join_rejects_traversal_segment(self, tmp_path, monkeypatch):
        """_validate_agent_name rejects '..'; _safe_join is the second line of defence."""
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        self._empty_local(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )

        # ValueError comes from _validate_agent_name; traversal never reaches the filesystem.
        with pytest.raises(ValueError, match="Invalid agent name"):
            load_agent_profile("../escaped")


class TestListAgentProfilesEdgeCases:
    """Test list_agent_profiles edge cases for coverage."""

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_builtin_profile_with_parse_error_still_added(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_dirs, mock_get_extra
    ):
        """Built-in profile with bad frontmatter should still be added with empty description."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        mock_file = MagicMock()
        mock_file.name = "broken.md"
        mock_file.read_text.side_effect = Exception("read error")
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = [mock_file]
        mock_resources.files.return_value = mock_agent_store

        mock_local_dir.exists.return_value = False

        result = list_agent_profiles()

        names = [p["name"] for p in result]
        assert "broken" in names
        broken_profile = [p for p in result if p["name"] == "broken"][0]
        assert broken_profile["description"] == ""
        assert broken_profile["source"] == "built-in"

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_builtin_store_exception_handled(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_dirs, mock_get_extra
    ):
        """Exception scanning built-in store should be caught, not crash."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        mock_resources.files.side_effect = Exception("No built-in store")
        mock_local_dir.exists.return_value = False

        result = list_agent_profiles()

        assert result == []

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs")
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_skips_provider_dir_same_as_local(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_dirs, mock_get_extra
    ):
        """Provider directory that matches local store should be skipped."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        mock_resources.files.return_value = MagicMock(iterdir=MagicMock(return_value=[]))

        # Use a real, resolved path so Path(dir_path).resolve() matches
        # the mock's resolve() return value (avoids macOS symlink issues
        # where e.g. /home -> /System/Volumes/Data/home).
        resolved_path = Path(__file__).resolve().parent
        mock_local_dir.exists.return_value = True
        mock_local_dir.resolve.return_value = resolved_path
        # Dedup now keys on _normalized_path(str(dir)) rather than .resolve(),
        # so give the mocked local store a real string form to match against.
        mock_local_dir.__str__.return_value = str(resolved_path)

        # Provider dir resolves to same as local
        mock_get_dirs.return_value = {"kiro_cli": str(resolved_path)}

        list_agent_profiles()

        # _scan_directory should be called for local but NOT for kiro_cli
        scan_calls = [c[0][1] for c in mock_scan.call_args_list]
        assert "local" in scan_calls
        assert "kiro" not in scan_calls

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs")
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_extra_dirs_scanned(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_dirs, mock_get_extra
    ):
        """Extra user directories should be scanned with 'custom' label."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        mock_resources.files.return_value = MagicMock(iterdir=MagicMock(return_value=[]))
        mock_local_dir.exists.return_value = False
        mock_get_extra.return_value = ["/extra/dir1", "/extra/dir2"]

        list_agent_profiles()

        custom_calls = [c for c in mock_scan.call_args_list if c[0][1] == "custom"]
        assert len(custom_calls) == 2

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_non_md_builtin_files_skipped(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_dirs, mock_get_extra
    ):
        """Non-md files in built-in store should be ignored."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        mock_py_file = MagicMock()
        mock_py_file.name = "__init__.py"
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = [mock_py_file]
        mock_resources.files.return_value = mock_agent_store
        mock_local_dir.exists.return_value = False

        result = list_agent_profiles()

        assert result == []


class TestF497CompositionStoresNotProfiles:
    """F558 #413: positions/ and overlays/ must never surface as fake profiles.

    On a fresh ``./install.sh`` the agent store gains sibling dirs
    ``positions/`` and ``overlays/`` (F497 composition inputs, see
    constants.py). Before the fix, ``_scan_directory`` recorded every
    subdirectory — even one without ``agent.md`` — as a listable profile, so
    ``list_agent_profiles()`` returned bogus "positions"/"overlays" entries.
    The session brief's ``profiles`` section then called
    ``_charter_projection("positions")`` -> ``read_agent_profile_source`` ->
    ``FileNotFoundError`` -> the ``profiles`` section errored ->
    terminal_service raised ``required session brief core section failed:
    profiles`` and every claude_code launch died on a fresh install.
    """

    @staticmethod
    def _fresh_install_store(tmp_path, monkeypatch):
        """Build a store that mirrors a fresh install and isolate discovery to it.

        Layout: positions/ + overlays/ (F497 sibling dirs, no agent.md), a
        stray non-.md top-level file, and 2 real flat profiles. Provider/extra
        dirs are emptied and the built-in store is stubbed empty so
        list_agent_profiles() reflects only this store.
        """
        store = tmp_path / "agent-store"
        store.mkdir()
        # F497 composition-input sibling dirs (no agent.md) — the crash source.
        (store / "positions").mkdir()
        (store / "positions" / "supervisor.md").write_text(
            "---\nrole: supervisor\n---\nposition body"
        )
        (store / "overlays").mkdir()
        (store / "overlays" / "claude_code.md").write_text("---\n---\noverlay body")
        # A stray top-level non-.md file must also be ignored.
        (store / "README.txt").write_text("not a profile")
        # Two real, loadable flat profiles.
        (store / "developer.md").write_text(
            "---\nname: developer\ndescription: Dev agent\n---\nDev prompt"
        )
        (store / "reviewer.md").write_text(
            "---\nname: reviewer\ndescription: Review agent\n---\nReview prompt"
        )

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", store
        )
        empty_builtin = tmp_path / "builtin"
        empty_builtin.mkdir()
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.resources.files",
            lambda _pkg: empty_builtin,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs", lambda: []
        )
        return store

    def test_positions_overlays_stray_excluded_only_real_profiles(self, tmp_path, monkeypatch):
        """Store with positions/, overlays/, a stray file + 2 real profiles -> exactly 2.

        Mutation note: revert the ``if not agent_md.exists(): continue`` guard
        in ``_scan_directory`` (record bare dirs as listable). ``names`` then
        gains "positions" and "overlays" and this assertion fails.
        """
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        self._fresh_install_store(tmp_path, monkeypatch)

        names = sorted(p["name"] for p in list_agent_profiles())

        assert names == ["developer", "reviewer"]
        assert "positions" not in names
        assert "overlays" not in names

    def test_brief_profiles_section_builds_over_fresh_install_store(self, tmp_path, monkeypatch):
        """Regression: the session-brief profiles loop no longer raises FileNotFoundError.

        This drives the exact loop ``session_manifest_service.build_session_manifest``
        runs for its ``profiles`` section: for every name from
        ``list_agent_profiles()`` it calls ``_charter_projection(name)``. Before
        the fix a bogus "positions"/"overlays" name raised FileNotFoundError
        here, erroring the section and crashing the required brief.

        Mutation note: revert the ``_scan_directory`` guard and this raises
        FileNotFoundError on the "positions"/"overlays" projection, failing the
        test exactly as the fresh-install launch failed.
        """
        from cli_agent_orchestrator.services.session_manifest_service import _charter_projection
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        self._fresh_install_store(tmp_path, monkeypatch)

        rows = [_charter_projection(item["name"]) for item in list_agent_profiles()]

        assert sorted(r["name"] for r in rows) == ["developer", "reviewer"]
