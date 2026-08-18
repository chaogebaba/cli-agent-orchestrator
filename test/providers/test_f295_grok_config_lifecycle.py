"""F295 Half 1: grok config lifecycle tests.

AC0 — rebuild-per-launch
AC2 — staleness stamp
AC4 — debounced change notice
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_cao_home(tmp_path):
    """Override CAO_HOME_DIR to an isolated temp directory."""
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        yield tmp_path


@pytest.fixture
def grok_plane(tmp_cao_home):
    """Set up a mock grok provider plane."""
    plane = MagicMock()
    plane.home = tmp_cao_home / "grok_plane"
    plane.home.mkdir(parents=True, exist_ok=True)
    plane.sessions = tmp_cao_home / "grok_plane" / "sessions"
    plane.sessions.mkdir(parents=True, exist_ok=True)
    plane.credential_path = tmp_cao_home / "grok_plane" / "auth.json"
    plane.credential_path.write_text("{}")
    return plane


@pytest.fixture
def provider(tmp_cao_home, grok_plane):
    """Create a GrokCliProvider with mocked external deps."""
    canonical = grok_plane.home / "config.toml"
    canonical.write_text('[model."grok-4.6"]\nname = "test"\n')

    with (
        patch("cli_agent_orchestrator.providers.grok_cli.provider_home") as mock_plane,
        patch("cli_agent_orchestrator.providers.grok_cli.get_backend") as mock_backend,
    ):
        mock_plane.return_value = grok_plane

        backend_inst = MagicMock()
        backend_inst.get_pane_working_directory.return_value = "/tmp"
        mock_backend.return_value = backend_inst

        p = GrokCliProvider(
            terminal_id="test-f295",
            session_name="s1",
            window_name="w1",
            agent_profile="grok_dev",
            allowed_tools=["*"],
        )
        yield p


@pytest.fixture
def provider_defaults_file(tmp_path, monkeypatch):
    path = tmp_path / "providers.toml"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.PROVIDER_DEFAULTS_FILE",
        path,
    )
    return path


# ---------------------------------------------------------------------------
# AC0: rebuild happens per launch
# ---------------------------------------------------------------------------


class TestAC0RebuildPerLaunch:
    """Config rebuild is anchored in _build_grok_command (launch path)."""

    def test_rebuild_updates_private_config_from_canonical(
        self, tmp_cao_home, grok_plane, provider_defaults_file
    ):
        """A changed canonical config propagates to private on build_command."""
        canonical = grok_plane.home / "config.toml"
        canonical.write_text('[model."grok-4.6"]\nname = "original"\n')

        with (
            patch("cli_agent_orchestrator.providers.grok_cli.provider_home") as mock_plane,
            patch("cli_agent_orchestrator.providers.grok_cli.get_backend") as mock_backend,
            patch("cli_agent_orchestrator.providers.grok_cli.load_agent_profile") as mock_profile,
            patch("cli_agent_orchestrator.clients.database.update_terminal_metadata") as mock_meta,
        ):
            mock_plane.return_value = grok_plane
            backend_inst = MagicMock()
            backend_inst.get_pane_working_directory.return_value = "/tmp"
            mock_backend.return_value = backend_inst
            mock_profile.return_value = None

            p = GrokCliProvider(
                terminal_id="rebuild-01",
                session_name="s1",
                window_name="w1",
                agent_profile="grok_dev",
                allowed_tools=["*"],
            )
            private_config = p._home_path() / "config.toml"
            assert private_config.read_text() == '[model."grok-4.6"]\nname = "original"\n'

            # Now change canonical
            canonical.write_text('[model."grok-4.6"]\nname = "updated"\n')

            # Build command triggers rebuild
            cmd = p._build_grok_command()

            assert private_config.read_text() == '[model."grok-4.6"]\nname = "updated"\n'
            assert "GROK_HOME" in cmd

    def test_malformed_canonical_keeps_prior_private_config(
        self, tmp_cao_home, grok_plane, provider_defaults_file
    ):
        """A malformed canonical does not clobber a working private config."""
        canonical = grok_plane.home / "config.toml"
        canonical.write_text('[model."grok-4.6"]\nname = "good"\n')

        with (
            patch("cli_agent_orchestrator.providers.grok_cli.provider_home") as mock_plane,
            patch("cli_agent_orchestrator.providers.grok_cli.get_backend") as mock_backend,
            patch("cli_agent_orchestrator.providers.grok_cli.load_agent_profile") as mock_profile,
            patch("cli_agent_orchestrator.clients.database.update_terminal_metadata") as mock_meta,
        ):
            mock_plane.return_value = grok_plane
            backend_inst = MagicMock()
            backend_inst.get_pane_working_directory.return_value = "/tmp"
            mock_backend.return_value = backend_inst
            mock_profile.return_value = None

            p = GrokCliProvider(
                terminal_id="rebuild-02",
                session_name="s1",
                window_name="w1",
                agent_profile="grok_dev",
                allowed_tools=["*"],
            )
            private_config = p._home_path() / "config.toml"
            assert "good" in private_config.read_text()

            # Corrupt canonical
            canonical.write_text('[model."grok-4.6"\nname = BROKEN TOML')

            # Build command — should keep prior config
            p._build_grok_command()
            assert "good" in private_config.read_text()
            assert "BROKEN" not in private_config.read_text()

    def test_missing_canonical_keeps_prior_private_config(
        self, tmp_cao_home, grok_plane, provider_defaults_file
    ):
        """If canonical is deleted, private config is preserved."""
        canonical = grok_plane.home / "config.toml"
        canonical.write_text('[model."grok-4.6"]\nname = "exists"\n')

        with (
            patch("cli_agent_orchestrator.providers.grok_cli.provider_home") as mock_plane,
            patch("cli_agent_orchestrator.providers.grok_cli.get_backend") as mock_backend,
            patch("cli_agent_orchestrator.providers.grok_cli.load_agent_profile") as mock_profile,
            patch("cli_agent_orchestrator.clients.database.update_terminal_metadata") as mock_meta,
        ):
            mock_plane.return_value = grok_plane
            backend_inst = MagicMock()
            backend_inst.get_pane_working_directory.return_value = "/tmp"
            mock_backend.return_value = backend_inst
            mock_profile.return_value = None

            p = GrokCliProvider(
                terminal_id="rebuild-03",
                session_name="s1",
                window_name="w1",
                agent_profile="grok_dev",
                allowed_tools=["*"],
            )
            private_config = p._home_path() / "config.toml"
            assert "exists" in private_config.read_text()

            # Remove canonical
            canonical.unlink()

            # Build command — should keep prior config
            p._build_grok_command()
            assert "exists" in private_config.read_text()

    def test_mcp_sections_survive_rebuild(self, tmp_cao_home, grok_plane, provider_defaults_file):
        """MCP sections upserted after rebuild are not lost."""
        canonical = grok_plane.home / "config.toml"
        canonical.write_text('[model."grok-4.6"]\nname = "base"\n')

        with (
            patch("cli_agent_orchestrator.providers.grok_cli.provider_home") as mock_plane,
            patch("cli_agent_orchestrator.providers.grok_cli.get_backend") as mock_backend,
            patch("cli_agent_orchestrator.providers.grok_cli.load_agent_profile") as mock_profile,
            patch("cli_agent_orchestrator.clients.database.update_terminal_metadata") as mock_meta,
        ):
            mock_plane.return_value = grok_plane
            backend_inst = MagicMock()
            backend_inst.get_pane_working_directory.return_value = "/tmp"
            mock_backend.return_value = backend_inst

            profile = SimpleNamespace(
                name="grok_dev",
                model=None,
                reasoningEffort=None,
                mcpServers={
                    "cao-mcp-server": {
                        "command": "/usr/bin/cao-mcp-server",
                        "args": [],
                        "env": {},
                    }
                },
                system_prompt=None,
            )
            mock_profile.return_value = profile

            p = GrokCliProvider(
                terminal_id="rebuild-04",
                session_name="s1",
                window_name="w1",
                agent_profile="grok_dev",
                allowed_tools=["*"],
            )

            # Build command — triggers rebuild then MCP upsert
            p._build_grok_command()

            private_config = p._home_path() / "config.toml"
            content = private_config.read_text()
            assert "base" in content  # canonical base preserved
            assert "cao-mcp-server" in content  # MCP section added on top


# ---------------------------------------------------------------------------
# AC2: staleness stamp written; stale computed
# ---------------------------------------------------------------------------


class TestAC2StalenessStamp:
    """sha256 stamped into metadata; fleet computes config_stale."""

    def test_stamp_written_on_rebuild(self, tmp_cao_home, grok_plane, provider_defaults_file):
        """Rebuild stamps sha256 of canonical text into metadata."""
        canonical = grok_plane.home / "config.toml"
        text = '[model."grok-4.6"]\nname = "stamp-test"\n'
        canonical.write_text(text)
        expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        with (
            patch("cli_agent_orchestrator.providers.grok_cli.provider_home") as mock_plane,
            patch("cli_agent_orchestrator.providers.grok_cli.get_backend") as mock_backend,
            patch("cli_agent_orchestrator.providers.grok_cli.load_agent_profile") as mock_profile,
            patch("cli_agent_orchestrator.clients.database.update_terminal_metadata") as mock_meta,
        ):
            mock_plane.return_value = grok_plane
            backend_inst = MagicMock()
            backend_inst.get_pane_working_directory.return_value = "/tmp"
            mock_backend.return_value = backend_inst
            mock_profile.return_value = None

            p = GrokCliProvider(
                terminal_id="stamp-01",
                session_name="s1",
                window_name="w1",
                agent_profile="grok_dev",
                allowed_tools=["*"],
            )
            p._build_grok_command()

            mock_meta.assert_called_with("stamp-01", {"config_sha256": expected_hash})

    def test_fleet_config_stale_true_when_hash_differs(self):
        """config_stale is True when stored hash != canonical hash."""
        from cli_agent_orchestrator.services.fleet_service import _is_config_stale

        row = {"provider": "grok_cli", "metadata": {"config_sha256": "aaa"}}
        assert _is_config_stale(row, "bbb") is True

    def test_fleet_config_stale_false_when_hash_matches(self):
        """config_stale is False when stored hash == canonical hash."""
        from cli_agent_orchestrator.services.fleet_service import _is_config_stale

        row = {"provider": "grok_cli", "metadata": {"config_sha256": "aaa"}}
        assert _is_config_stale(row, "aaa") is False

    def test_fleet_config_stale_none_for_non_grok(self):
        """config_stale is None for non-grok providers."""
        from cli_agent_orchestrator.services.fleet_service import _is_config_stale

        row = {"provider": "codex", "metadata": {"config_sha256": "aaa"}}
        assert _is_config_stale(row, "bbb") is None

    def test_fleet_config_stale_none_when_no_metadata(self):
        """config_stale is None when terminal has no metadata."""
        from cli_agent_orchestrator.services.fleet_service import _is_config_stale

        row = {"provider": "grok_cli", "metadata": None}
        assert _is_config_stale(row, "bbb") is None

    def test_fleet_config_stale_none_when_no_canonical(self):
        """config_stale is None when canonical hash is None."""
        from cli_agent_orchestrator.services.fleet_service import _is_config_stale

        row = {"provider": "grok_cli", "metadata": {"config_sha256": "aaa"}}
        assert _is_config_stale(row, None) is None


# ---------------------------------------------------------------------------
# AC4: debounced change notice
# ---------------------------------------------------------------------------


class TestAC4ChangeNotice:
    """Watcher debounces and notifies on change."""

    def test_no_notice_on_same_hash(self, tmp_path):
        """If mtime changes but content hash is the same, no notice fires."""
        from cli_agent_orchestrator.services.grok_config_watcher import GrokConfigWatcher

        config = tmp_path / "config.toml"
        config.write_text('[model]\nname = "stable"\n')

        with (
            patch(
                "cli_agent_orchestrator.services.grok_config_watcher._canonical_config_path",
                return_value=config,
            ),
            patch(
                "cli_agent_orchestrator.services.grok_config_watcher._push_supervisor_notice"
            ) as mock_push,
        ):
            watcher = GrokConfigWatcher()
            watcher._snapshot_baseline()

            # Touch mtime without changing content
            import os
            import time

            time.sleep(0.01)
            os.utime(config, (time.time() + 1, time.time() + 1))

            watcher._check_and_notify()
            mock_push.assert_not_called()

    def test_notice_on_content_change(self, tmp_path):
        """Content change triggers exactly one notice."""
        from cli_agent_orchestrator.services.grok_config_watcher import GrokConfigWatcher

        config = tmp_path / "config.toml"
        config.write_text('[model]\nname = "before"\n')

        with (
            patch(
                "cli_agent_orchestrator.services.grok_config_watcher._canonical_config_path",
                return_value=config,
            ),
            patch(
                "cli_agent_orchestrator.services.grok_config_watcher._push_supervisor_notice"
            ) as mock_push,
            patch(
                "cli_agent_orchestrator.services.grok_config_watcher._count_stale_grok_terminals",
                return_value=2,
            ),
        ):
            watcher = GrokConfigWatcher()
            watcher._snapshot_baseline()

            # Change content
            config.write_text('[model]\nname = "after"\n')

            watcher._check_and_notify()
            mock_push.assert_called_once()
            args = mock_push.call_args[0]
            # First arg is the new hash
            expected_hash = hashlib.sha256('[model]\nname = "after"\n'.encode()).hexdigest()
            assert args[0] == expected_hash
            # Second arg is stale count
            assert args[1] == 2

    def test_debounce_no_duplicate_notice_same_mtime(self, tmp_path):
        """Subsequent polls with same mtime do not fire another notice."""
        from cli_agent_orchestrator.services.grok_config_watcher import GrokConfigWatcher

        config = tmp_path / "config.toml"
        config.write_text('[model]\nname = "v1"\n')

        with (
            patch(
                "cli_agent_orchestrator.services.grok_config_watcher._canonical_config_path",
                return_value=config,
            ),
            patch(
                "cli_agent_orchestrator.services.grok_config_watcher._push_supervisor_notice"
            ) as mock_push,
            patch(
                "cli_agent_orchestrator.services.grok_config_watcher._count_stale_grok_terminals",
                return_value=1,
            ),
        ):
            watcher = GrokConfigWatcher()
            watcher._snapshot_baseline()

            # Change content
            config.write_text('[model]\nname = "v2"\n')

            watcher._check_and_notify()
            assert mock_push.call_count == 1

            # Poll again — same mtime — no new notice
            watcher._check_and_notify()
            assert mock_push.call_count == 1

    def test_deleted_canonical_no_crash(self, tmp_path):
        """Deleted canonical is handled gracefully — no notice, no crash."""
        from cli_agent_orchestrator.services.grok_config_watcher import GrokConfigWatcher

        config = tmp_path / "config.toml"
        config.write_text('[model]\nname = "exists"\n')

        with (
            patch(
                "cli_agent_orchestrator.services.grok_config_watcher._canonical_config_path",
                return_value=config,
            ),
            patch(
                "cli_agent_orchestrator.services.grok_config_watcher._push_supervisor_notice"
            ) as mock_push,
        ):
            watcher = GrokConfigWatcher()
            watcher._snapshot_baseline()

            config.unlink()

            watcher._check_and_notify()
            mock_push.assert_not_called()
