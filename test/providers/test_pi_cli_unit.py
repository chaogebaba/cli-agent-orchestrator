"""Unit tests for the Pi CLI provider (persistent regular-TUI in tmux).

Tests cover:
  - Command construction (flags, model/thinking resolution, tool exclusion)
  - MCP config materialization (cao-mcp-server identity injection, no /data)
  - Status detection from Pi TUI chrome (idle/processing/completed/error)
  - Response extraction from scrollback
  - Cleanup guard (only removes the worker's own runtime subdir)
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.pi_cli import (
    _FOOTER_CONTEXT,
    _PI_MCP_TIMEOUT_MS_FLOOR,
    PI_BINARY,
    PI_RUNTIME_ROOT,
    PiCliProvider,
    _resolve_pi_mcp_timeout_ms,
)
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ─── Registration ──────────────────────────────────────────────────────────────


def test_provider_registered() -> None:
    """pi_cli is in the enum and resolves to PiCliProvider."""
    from cli_agent_orchestrator.providers.manager import get_provider_class

    assert ProviderType.PI_CLI.value == "pi_cli"
    assert get_provider_class("pi_cli") is PiCliProvider


def test_pi_binary_is_absolute() -> None:
    """The launch path is absolute (cao-server runs from systemd, no ~/.bun PATH)."""
    assert PI_BINARY.endswith("/.bun/bin/pi")
    assert Path(PI_BINARY).is_absolute()


# ─── Command construction ───────────────────────────────────────────────────────


class TestCommandConstruction:
    """Tests for _build_pi_command flag assembly and resolution."""

    def _provider(self, **kwargs) -> PiCliProvider:
        return PiCliProvider("t1234567", "sess", "win0", **kwargs)

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_core_flags_present(self, mock_defaults) -> None:
        mock_defaults.return_value = {
            "model": "cline-pass/glm-5.3-flash",
            "reasoning_effort": "high",
        }
        provider = self._provider()
        parts = shlex.split(provider._build_pi_command())
        assert parts[0] == PI_BINARY
        assert "--tui-mode" in parts and parts[parts.index("--tui-mode") + 1] == "regular"
        assert "--no-approve" in parts
        assert "--no-context-files" in parts
        assert "--session-id" in parts and "t1234567" in parts
        assert "--append-system-prompt" in parts
        assert "--mcp-config" in parts
        # --no-extensions disables the adapter that registers --mcp-config, so it
        # must never be passed (see build report §1.2). Lock the hazard closed.
        assert "--no-extensions" not in parts

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_model_and_thinking_resolved(self, mock_defaults) -> None:
        mock_defaults.return_value = {
            "model": "cline-pass/glm-5.3-flash",
            "reasoning_effort": "high",
        }
        provider = self._provider()
        parts = shlex.split(provider._build_pi_command())
        assert parts[parts.index("--model") + 1] == "cline-pass/glm-5.3-flash"
        assert parts[parts.index("--thinking") + 1] == "high"
        assert provider.resolved_model == "cline-pass/glm-5.3-flash"
        assert provider.resolved_reasoning_effort == "high"

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_spawn_model_override_wins(self, mock_defaults) -> None:
        """An explicit model kwarg (assign/handoff) overrides providers.toml."""
        mock_defaults.return_value = {
            "model": "cline-pass/glm-5.3-flash",
            "reasoning_effort": "high",
        }
        provider = self._provider(model="cline-pass/kimi-k3")
        parts = shlex.split(provider._build_pi_command())
        assert parts[parts.index("--model") + 1] == "cline-pass/kimi-k3"
        assert provider.resolved_model == "cline-pass/kimi-k3"

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_thinking_empty_clears_flag(self, mock_defaults) -> None:
        """An explicit empty reasoning_effort suppresses --thinking (persisted None)."""
        mock_defaults.return_value = {"model": "cline-pass/glm-5.3-flash", "reasoning_effort": ""}
        provider = self._provider()
        parts = shlex.split(provider._build_pi_command())
        assert "--thinking" not in parts
        assert provider.resolved_reasoning_effort is None

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_tool_exclusion_for_restricted_worker(self, mock_defaults) -> None:
        """A restricted allowlist produces a --exclude-tools denylist."""
        mock_defaults.return_value = {
            "model": "cline-pass/glm-5.3-flash",
            "reasoning_effort": "high",
        }
        provider = self._provider(allowed_tools=["fs_read"])
        parts = shlex.split(provider._build_pi_command())
        assert "--exclude-tools" in parts
        excluded = parts[parts.index("--exclude-tools") + 1].split(",")
        # bash/edit/write are execution/write tools; must be blocked for a read-only worker.
        assert "bash" in excluded and "write" in excluded and "edit" in excluded
        # read/ls are allowed by fs_read and must NOT be excluded.
        assert "read" not in excluded and "ls" not in excluded

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_unrestricted_worker_no_exclude(self, mock_defaults) -> None:
        mock_defaults.return_value = {
            "model": "cline-pass/glm-5.3-flash",
            "reasoning_effort": "high",
        }
        provider = self._provider(allowed_tools=["*"])
        parts = shlex.split(provider._build_pi_command())
        assert "--exclude-tools" not in parts


# ─── MCP config materialization ─────────────────────────────────────────────────


class TestMcpConfig:
    """Tests for the per-worker mcp.json (the send_message callback path)."""

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.pi_cli.resolve_cao_mcp_command")
    @patch("cli_agent_orchestrator.utils.http.resolve_endpoint")
    def test_mcp_config_has_cao_server_and_identity(
        self, mock_endpoint, mock_resolve, mock_defaults, tmp_path, monkeypatch
    ) -> None:
        mock_defaults.return_value = {}
        mock_endpoint.return_value = "http://127.0.0.1:8999"
        mock_resolve.return_value = ("/usr/local/bin/cao-mcp-server", [])
        monkeypatch.setenv("CAO_TERMINAL_TOKEN", "tok-abc")
        provider = PiCliProvider("tABCDEF1", "sess", "win0")
        provider.runtime_dir = tmp_path / "tABCDEF1"
        provider.mcp_config_path = provider.runtime_dir / "mcp.json"

        path = provider._write_mcp_config()
        cfg = json.loads(path.read_text())
        server = cfg["mcpServers"]["cao-mcp-server"]
        assert server["command"] == "/usr/local/bin/cao-mcp-server"
        assert server["env"]["CAO_TERMINAL_ID"] == "tABCDEF1"
        assert server["env"]["CAO_TERMINAL_TOKEN"] == "tok-abc"
        assert server["env"]["CAO_ENDPOINT"] == "http://127.0.0.1:8999"
        assert cfg["settings"]["requestTimeoutMs"] >= _PI_MCP_TIMEOUT_MS_FLOOR

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.pi_cli.resolve_cao_mcp_command")
    @patch("cli_agent_orchestrator.utils.http.resolve_endpoint")
    def test_message_path_is_stdio_http_not_files(
        self, mock_endpoint, mock_resolve, mock_defaults, tmp_path
    ) -> None:
        """AC5: the callback path is an MCP stdio->HTTP command, not a /data file drop.

        The message/callback transport is cao-mcp-server spoken over stdio, whose
        env carries CAO_ENDPOINT (HTTP). No mailbox file, FIFO, or spool under
        /data or /tmp is part of the delivery path -- the mcp.json merely names
        the command to launch.
        """
        mock_defaults.return_value = {}
        mock_endpoint.return_value = "http://127.0.0.1:8999"
        mock_resolve.return_value = ("/usr/local/bin/cao-mcp-server", [])
        provider = PiCliProvider("t7654321", "sess", "win0")
        provider.runtime_dir = tmp_path / "t7654321"
        provider.mcp_config_path = provider.runtime_dir / "mcp.json"
        cfg = json.loads(provider._write_mcp_config().read_text())
        server = cfg["mcpServers"]["cao-mcp-server"]
        # stdio transport: a command is launched, no url/file/pipe field.
        assert "command" in server and "url" not in server
        assert server["env"]["CAO_ENDPOINT"].startswith("http")
        # No arg or env value routes delivery through /data or /tmp.
        blob = json.dumps(server)
        assert "/data" not in blob and "/tmp" not in blob

    def test_runtime_root_relocates_with_cao_home(self) -> None:
        """AC5: the config root derives from CAO_HOME_DIR (relocatable), never a
        hardcoded /data or /tmp literal in the source."""
        provider = PiCliProvider("t7654321", "sess", "win0")
        assert str(provider.mcp_config_path).startswith(str(PI_RUNTIME_ROOT))
        assert provider.mcp_config_path.name == "mcp.json"


# ─── providers.toml timeout knob ────────────────────────────────────────────────


class TestMcpTimeoutKnob:
    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_default(self, mock_defaults) -> None:
        mock_defaults.return_value = {}
        assert _resolve_pi_mcp_timeout_ms() == 60_000

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_override(self, mock_defaults) -> None:
        mock_defaults.return_value = {"mcp_request_timeout_ms": 90_000}
        assert _resolve_pi_mcp_timeout_ms() == 90_000

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_floored(self, mock_defaults) -> None:
        mock_defaults.return_value = {"mcp_request_timeout_ms": 1_000}
        assert _resolve_pi_mcp_timeout_ms() == _PI_MCP_TIMEOUT_MS_FLOOR


# ─── Status detection ───────────────────────────────────────────────────────────


class TestStatusDetection:
    """Tests for get_status against real Pi TUI pane fixtures.

    _resolve_native_status is patched to None so these exercise the TUI-chrome
    path (the tmux path, where native status is always None).
    """

    def _provider(self, initialized=True, dispatched=False, processing_seen=False) -> PiCliProvider:
        provider = PiCliProvider("t1234567", "sess", "win0")
        provider._initialized = initialized
        provider._task_dispatched = dispatched
        provider._tui_processing_seen = processing_seen
        return provider

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_idle_before_dispatch(self, _native) -> None:
        provider = self._provider(dispatched=False)
        assert provider.get_status(_fixture("pi_idle.txt")) == TerminalStatus.IDLE

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_idle_chrome_survives_trailing_blank_padding(self, _native) -> None:
        """F798 r2 B-1 regression: pi's short-conversation TUI renders the
        composer/footer near the TOP and pads the pane with ~24 trailing blank
        lines. The real box capture (``pi_idle_padded.txt``, 50 lines, 24 of them
        trailing blanks) must still read IDLE — the old ``lines[-25:]`` tail
        landed entirely on the blank pad and missed the chrome, so a
        server-issued pi worker timed out at init despite a healthy idle frame.
        ``_has_idle_chrome`` now strips trailing whitespace-only lines before the
        tail window.
        """
        raw = _fixture("pi_idle_padded.txt")
        clean = strip_terminal_escapes(raw)
        # Precondition: the frame really does end in a run of blank lines.
        trailing_blanks = 0
        for line in reversed(clean.splitlines()):
            if line.strip():
                break
            trailing_blanks += 1
        assert trailing_blanks >= 20, f"fixture lost its padding: {trailing_blanks}"
        assert PiCliProvider._has_idle_chrome(clean) is True
        provider = self._provider(dispatched=False)
        assert provider.get_status(raw) == TerminalStatus.IDLE

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_padded_frame_without_footer_is_not_idle(self, _native) -> None:
        """Same padded frame with the ``%/…(auto)`` footer line removed must NOT
        read idle — proves the trailing-blank strip did not weaken the footer
        requirement into a bare rule-count match.
        """
        clean = strip_terminal_escapes(_fixture("pi_idle_padded.txt"))
        no_footer = "\n".join(
            line for line in clean.splitlines() if not _FOOTER_CONTEXT.search(line)
        )
        assert PiCliProvider._has_idle_chrome(no_footer) is False

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_processing_working_spinner(self, _native) -> None:
        provider = self._provider(dispatched=True)
        assert provider.get_status(_fixture("pi_processing.txt")) == TerminalStatus.PROCESSING

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_completed_after_dispatch(self, _native) -> None:
        """Idle chrome + task dispatched + a processing frame seen → COMPLETED."""
        provider = self._provider(dispatched=True, processing_seen=True)
        assert provider.get_status(_fixture("pi_completed.txt")) == TerminalStatus.COMPLETED

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_idle_after_dispatch_without_processing_frame(self, _native) -> None:
        """Idle chrome with a dispatch but no processing frame yet stays IDLE (not COMPLETED)."""
        provider = self._provider(dispatched=True, processing_seen=False)
        assert provider.get_status(_fixture("pi_idle.txt")) == TerminalStatus.IDLE

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_error_startup_banner(self, _native) -> None:
        provider = self._provider(dispatched=False)
        assert provider.get_status(_fixture("pi_error.txt")) == TerminalStatus.ERROR

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_uninitialized_unknown(self, _native) -> None:
        provider = self._provider(initialized=False)
        assert provider.get_status(_fixture("pi_idle.txt")) == TerminalStatus.UNKNOWN

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_empty_buffer_unknown(self, _native) -> None:
        provider = self._provider()
        assert provider.get_status("") == TerminalStatus.UNKNOWN

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_processing_takes_priority_over_idle_chrome(self, _native) -> None:
        """A frame with BOTH old idle chrome and a new Working spinner → PROCESSING."""
        provider = self._provider(dispatched=True)
        buffer = _fixture("pi_idle.txt") + "\n── \u283f Working ──────────────────\n"
        assert provider.get_status(buffer) == TerminalStatus.PROCESSING

    def test_native_status_wins_when_present(self) -> None:
        """When the backend resolves a native status, TUI parsing is skipped."""
        provider = self._provider(dispatched=True, processing_seen=True)
        with patch.object(
            PiCliProvider, "_resolve_native_status", return_value=TerminalStatus.PROCESSING
        ):
            assert provider.get_status(_fixture("pi_completed.txt")) == TerminalStatus.PROCESSING

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_empty_pushed_buffer_live_reads_idle_pane(self, _native) -> None:
        """F798 r2 B-1 fix #2: pi's persistent TUI does not feed the tmux FIFO
        push buffer at rest, so a parked worker's pushed buffer is empty. When it
        is, _resolve_buffer must do ONE live pane read and classify THAT — an
        empty buffer over an idle pane is IDLE (not UNKNOWN), else the
        IDLE-gated inbox delivery never fires and callbacks are lost.
        """
        provider = self._provider(dispatched=False)
        with patch.object(
            PiCliProvider, "_read_pane", return_value=_fixture("pi_idle_padded.txt")
        ) as read:
            assert provider.get_status("") == TerminalStatus.IDLE
            read.assert_called_once()

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_empty_pushed_buffer_live_reads_non_idle_pane(self, _native) -> None:
        """Empty pushed buffer + a live pane with no idle chrome → NOT idle
        (UNKNOWN here): the live-read fallback must not manufacture readiness.
        """
        provider = self._provider(dispatched=False)
        with patch.object(
            PiCliProvider, "_read_pane", return_value="just a shell prompt $ "
        ) as read:
            assert provider.get_status("") == TerminalStatus.UNKNOWN
            read.assert_called_once()

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_non_empty_pushed_buffer_skips_live_read(self, _native) -> None:
        """A non-empty pushed buffer is classified as-is — no extra pane read
        (no backend round-trip when there is already content to classify).
        """
        provider = self._provider(dispatched=False)
        with patch.object(PiCliProvider, "_read_pane") as read:
            assert provider.get_status(_fixture("pi_idle.txt")) == TerminalStatus.IDLE
            read.assert_not_called()


# ─── Response extraction ────────────────────────────────────────────────────────


class TestMessageExtraction:
    def _provider(self) -> PiCliProvider:
        return PiCliProvider("t1234567", "sess", "win0")

    def test_extract_completed_response(self) -> None:
        """The last assistant block above the composer rules is extracted."""
        result = self._provider().extract_last_message_from_script(_fixture("pi_completed.txt"))
        assert result
        assert "Working" not in result
        # The completed fixture ends with a numbered sentence about "40".
        assert "40" in result

    def test_extract_raises_while_working(self) -> None:
        with pytest.raises(ValueError):
            self._provider().extract_last_message_from_script(_fixture("pi_processing.txt"))

    def test_extract_raises_on_banner_only(self) -> None:
        with pytest.raises(ValueError):
            self._provider().extract_last_message_from_script("pi v0.85.1\nwarming up\n")


# ─── Cleanup ────────────────────────────────────────────────────────────────────


class TestCleanup:
    def test_cleanup_removes_own_runtime_dir(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", tmp_path)
        provider = PiCliProvider("tCLEAN01", "sess", "win0")
        provider.runtime_dir = tmp_path / "tCLEAN01"
        provider.runtime_dir.mkdir(parents=True)
        (provider.runtime_dir / "mcp.json").write_text("{}")
        provider.cleanup()
        assert not provider.runtime_dir.exists()

    def test_cleanup_guard_refuses_foreign_dir(self, tmp_path, monkeypatch) -> None:
        """The guard refuses to rmtree a dir that is not <ROOT>/<terminal_id>."""
        monkeypatch.setattr("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", tmp_path)
        provider = PiCliProvider("tCLEAN02", "sess", "win0")
        foreign = tmp_path / "somewhere-else"
        foreign.mkdir(parents=True)
        provider.runtime_dir = foreign  # name != terminal_id
        provider.cleanup()
        assert foreign.exists()  # untouched


# ─── Misc contract ──────────────────────────────────────────────────────────────


def test_exit_cli_is_ctrl_d() -> None:
    assert PiCliProvider("t1234567", "s", "w").exit_cli() == "C-d"


def test_paste_enter_count_is_one() -> None:
    assert PiCliProvider("t1234567", "s", "w").paste_enter_count == 1
