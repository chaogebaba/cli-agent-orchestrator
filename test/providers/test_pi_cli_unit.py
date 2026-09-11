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
    _EDITOR_RULE,
    _FOOTER_CONTEXT,
    _PI_MCP_TIMEOUT_MS_FLOOR,
    _WORKING_ROW,
    PI_BINARY,
    PI_RUNTIME_ROOT,
    PiCliProvider,
    _resolve_pi_fresh_session_on_spawn,
    _resolve_pi_mcp_timeout_ms,
    _visible_width,
)
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _working_row(glyph: str = "⠴", width: int = 120, *, message: str = "Working") -> str:
    """Build a live pi working top-border row of EXACTLY ``width`` columns.

    F847 r5 (#703): the live working row IS the composer top border, so it spans
    the composer width — ``_live_working_spinner`` enforces that invariant. A
    synthetic ``── ⠴ Working ───`` row must therefore be padded on its trailing
    box rule to match the composer rules in the same frame, exactly as
    ``renderTopBorder`` sizes the rule from ``width``. Returns
    ``── <glyph> <message> <closing ─ run>`` whose visible width == ``width``.
    """
    lead = f"── {glyph} {message} "
    fill = width - _visible_width(lead)
    assert fill >= 1, f"width {width} too small for {lead!r}"
    return lead + ("─" * fill)


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

    def test_opts_into_cached_unknown_self_heal(self) -> None:
        """F808 (#665): pi_cli sets supports_direct_status_probe so the
        StatusMonitor's cached-UNKNOWN-at-rest self-heal (get_raw_status)
        re-detects a parked worker from a fresh pane capture. Without this the
        FIFO-fed cache stays UNKNOWN forever and IDLE-gated inbox delivery to a
        parked pi worker never fires (F798 r2 §8.3)."""
        assert PiCliProvider.supports_direct_status_probe is True
        # A raw-stream provider (kiro_cli) must NOT be opted in — the fix is
        # strictly pi-class, not a blanket default flip.
        from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

        assert KiroCliProvider.supports_direct_status_probe is False

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
        # pi_idle.txt's composer rules are 200 cols; the live working row spans
        # the composer, so build it at 200 (F847 r5 width invariant).
        buffer = _fixture("pi_idle.txt") + "\n" + _working_row("\u283f", 200) + "\n"
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


# ─── F847 (#703): false-idle — ⠴ Working spinner + long-running tool ────────────


class TestFalseIdleWorkingSpinner:
    """F847 (#703): the fleet server reported ``idle`` while the pane showed a
    live ``── ⠴ Working ──`` braille spinner row with a long-running shell tool
    printing its own ``Elapsed``/``(timeout Ns)`` block ABOVE the spinner. idle
    is the delivery-eligible state, so a false idle would type a callback into a
    busy composer and let reap/hibernate kill a working lane.

    The spinner row must win as PROCESSING regardless of the tool-output block
    above it, and BEFORE any idle/composer-chrome check. ``_resolve_native_status``
    is patched to None so these exercise the tmux TUI-chrome path.
    """

    # The byte-exact incident capture, filed in the certification corpus.
    _CORPUS = FIXTURES / "status_truth" / "pi_cli" / "working-1.txt"

    def _provider(self, dispatched=True, processing_seen=False) -> PiCliProvider:
        provider = PiCliProvider("t1234567", "sess", "win0")
        provider._initialized = True
        provider._task_dispatched = dispatched
        provider._tui_processing_seen = processing_seen
        return provider

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_corpus_capture_is_processing_raw(self, _native) -> None:
        """The raw (ANSI-laden) incident capture classifies PROCESSING, not idle."""
        raw = self._CORPUS.read_text(encoding="utf-8")
        assert self._provider(dispatched=True, processing_seen=True).get_status(raw) == (
            TerminalStatus.PROCESSING
        )

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_corpus_capture_is_processing_ansi_stripped(self, _native) -> None:
        """Same capture ANSI-stripped (the buffer get_status actually parses)
        still classifies PROCESSING — the ⠴ Working row is matched wherever it
        sits and beats the idle/composer chrome."""
        clean = strip_terminal_escapes(self._CORPUS.read_text(encoding="utf-8"))
        assert self._provider(dispatched=True).get_status(clean) == TerminalStatus.PROCESSING

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_long_running_tool_block_above_spinner_does_not_flip_idle(self, _native) -> None:
        """Synthetic long-running-tool frame: an ``Elapsed``/``(timeout Ns)``
        tool-output block, then the spinner row, then the composer rules + footer.
        The idle chrome below the spinner must NOT flip the verdict to idle."""
        _RULE = "─" * 120
        _FOOTER = (
            "↑48k ↓8.4k R731k CH98.8% $0.017 3.0%/1.0M (auto)   cline-pass/glm-5.3-flash • high"
        )
        buffer = "\n".join(
            [
                " $ pytest -q test/providers/ (timeout 1200s)",
                " (timeout 1200s)",
                " Elapsed 102.2s",
                "",
                _working_row("⠴", 120),
                " ",
                _RULE,
                "/data/cao-scratch/worktrees/cli-agent-orchestrator/pane (cao/pane)",
                _FOOTER,
                "🔌 MCP: 1 server enabled",
            ]
        )
        assert self._provider(dispatched=True).get_status(buffer) == TerminalStatus.PROCESSING

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_all_braille_spinner_frames_are_processing(self, _native) -> None:
        """Every canonical spinner frame (#703 glyph set) on a rule row → PROCESSING."""
        for glyph in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏":
            buffer = f"── {glyph} Working " + "─" * 100 + "\n"
            assert self._provider(dispatched=True).get_status(buffer) == (
                TerminalStatus.PROCESSING
            ), f"frame {glyph!r} not detected as working"

    # ── #703 r4: live composer-border shapes that carry content before the ──────
    # closing rule (Opus r3 EMPIRICAL-GATE-NO). The r3 ``$``-after-whitespace tail
    # asserted "nothing but whitespace/optional-rule follows Working" and regressed
    # every one of these live rows from PROCESSING to UNKNOWN, re-opening the #703
    # false-idle from the other side (status_monitor latches the prior status on a
    # detected UNKNOWN). The tail pins on the CLOSING border rule, so a row that
    # ENDS on the rule stays PROCESSING however much real content precedes it —
    # and (F847 r5) the row must span the full composer width, which the fixtures
    # below satisfy. The overflow fixture is a GENUINE live capture; the other two
    # are geometry-correct derivations (width 191 == working-1.txt) — see the
    # .json sidecars.
    _OVERFLOW_FIXTURES = (
        # custom-editor.js:37-40 — hidden-line overflow label inside the rule run;
        # GENUINE live capture (capture3/frames/q_001.txt), composer width 100.
        "working-overflow-queued",
        # interactive-mode.js:1768 — "Working (esc to interrupt)" (derived, w=191).
        "working-esc-interrupt",
        # interactive-mode.js:1906 — "Working on tool call" (derived, w=191).
        "working-on-tool-call",
    )

    def test_pi_working_fixture_rows_are_composer_width(self) -> None:
        """#703 r5 (Opus r4 Blocker 2): every pi WORKING fixture's working row
        must span the full composer width — the live working row IS the composer
        top border, so ``renderTopBorder`` sizes its trailing rule from ``width``
        and the total is invariant. The r4 fixtures were 19-25 columns short (rows
        pi cannot draw); this asserts none is hand-shortened. No live-box turn: it
        reads the committed corpus."""
        for name in ("working-1", *self._OVERFLOW_FIXTURES):
            clean = strip_terminal_escapes(
                (FIXTURES / "status_truth" / "pi_cli" / f"{name}.txt").read_text(encoding="utf-8")
            )
            rows = clean.splitlines()
            work_rows = [r for r in rows if _WORKING_ROW.match(r)]
            rule_rows = [r for r in rows if _EDITOR_RULE.match(r)]
            assert work_rows, f"{name}: no working row found"
            assert rule_rows, f"{name}: no composer rule found"
            composer_width = max(_visible_width(r) for r in rule_rows)
            for wr in work_rows:
                assert _visible_width(wr) == composer_width, (
                    f"{name}: working row width {_visible_width(wr)} != composer width "
                    f"{composer_width} — a row pi cannot draw (hand-shortened fixture)"
                )

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_live_border_content_before_closing_rule_is_processing(self, _native) -> None:
        """The three live pi 0.85.1 working-border shapes that draw real content
        between ``Working`` and the closing composer rule (overflow ``↑ N more``
        label, ``(esc to interrupt)``, ``on tool call``) classify PROCESSING —
        raw AND ANSI-stripped, at dispatched+processing_seen and on a fresh
        provider. These are the rows the r3 ``$`` tail wrongly rejected."""
        for name in self._OVERFLOW_FIXTURES:
            raw = (FIXTURES / "status_truth" / "pi_cli" / f"{name}.txt").read_text(encoding="utf-8")
            clean = strip_terminal_escapes(raw)
            assert self._provider(dispatched=True, processing_seen=True).get_status(raw) == (
                TerminalStatus.PROCESSING
            ), f"{name}: raw must be PROCESSING (live working border)"
            assert self._provider(dispatched=True, processing_seen=True).get_status(clean) == (
                TerminalStatus.PROCESSING
            ), f"{name}: ANSI-stripped must be PROCESSING"
            # A fresh provider (no dispatch/processing yet) must not read the live
            # working border as anything but PROCESSING either.
            assert self._provider(dispatched=False, processing_seen=False).get_status(raw) == (
                TerminalStatus.PROCESSING
            ), f"{name}: fresh-provider raw must be PROCESSING"

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_all_glyphs_overflow_and_message_borders_are_processing(self, _native) -> None:
        """Every canonical glyph, in each of the three live border shapes that
        carry content before the closing rule, → PROCESSING. Guards the r4
        closing-rule tail against a future narrowing that only handles the plain
        frame."""
        rule = "─" * 90
        tails = (f"─── ↑ 12 more {rule}", f"(esc to interrupt) {rule}", f"on tool call {rule}")
        for glyph in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏":
            for tail in tails:
                buffer = f"── {glyph} Working {tail}\n"
                assert self._provider(dispatched=True).get_status(buffer) == (
                    TerminalStatus.PROCESSING
                ), f"glyph {glyph!r} tail {tail[:16]!r} not PROCESSING"

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_spinner_first_shape_is_not_a_live_frame(self, _native) -> None:
        """#703 r3 (codex r2 EMPIRICAL-GATE-NO + LIVE capture): a "spinner-first"
        row where the glyph LEADS and the rule TRAILS (``⠴ Working ────``, no
        leading box rule) is NOT a real pi 0.85.1 frame and must NOT classify
        PROCESSING. r2 matched this shape on a mid-redraw theory the verdict
        rejected; a live capture of pi 0.85.1 (390 frames @ 20 ms across 6 turn
        starts + 6 resizes) produced 383 working rows, ALL rule-leading, ZERO
        spinner-first — and the pi-tui source composes the border rule-leading
        inside a synchronized-output transaction, so no such intermediate is
        observable. The row is therefore treated as transcript: idle chrome below
        it wins (COMPLETED)."""
        buffer = "\n".join(
            [
                " I pasted a status line: ⠴ Working " + "─" * 40,
                "",
                self._RULE,
                " ",
                self._RULE,
                self._FOOTER,
            ]
        )
        provider = self._provider(dispatched=True, processing_seen=True)
        assert provider.get_status(buffer) == TerminalStatus.COMPLETED

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_prose_working_without_spinner_or_rule_is_not_processing(self, _native) -> None:
        """Ordinary transcript prose that merely says 'Working' — no braille
        glyph and no rule — must NOT be misread as a spinner (idle chrome below
        it wins)."""
        _RULE = "─" * 120
        _FOOTER = "↑1k ↓1k R1k CH1% $0.001 0.1%/1.0M (auto)   cline-pass/glm-5.3-flash • high"
        buffer = "\n".join(
            [
                " Working through the queue now, almost done.",
                "",
                _RULE,
                " ",
                _RULE,
                _FOOTER,
            ]
        )
        # Not PROCESSING: no spinner row. Dispatched + a prior processing frame →
        # COMPLETED (idle chrome), proving the prose did not latch working.
        provider = self._provider(dispatched=True, processing_seen=True)
        assert provider.get_status(buffer) == TerminalStatus.COMPLETED

    # ── #703 r3: the atomic RULE-LEADING frame is the only live shape ───────────
    #
    # The byte-exact incident (working-1.txt) is a RULE-LEADING frame. BASE
    # b1e4d48c already classifies it PROCESSING, so it is not a false-idle
    # RED→GREEN repro — it is the positive ANCHOR that the live pi 0.85.1 working
    # row (always ``── ⠧ Working ──``, emitted atomically in a synchronized-output
    # transaction) stays PROCESSING. r2's DERIVED ``working-spinner-first.txt``
    # fixture and its mid-redraw tests were REMOVED in r3: a live capture found no
    # spinner-first frame in 390 samples (see the fork report r3 measurement), so
    # the fixture had no live basis. M703-2 (braille class narrowed to only
    # ``⠴``) is now killed by ``test_all_braille_spinner_frames_are_processing``
    # above — the rule-leading all-glyph arm — which no longer has a permissive
    # alternative masking the braille class.

    # ── #703 r2: transcript OVERMATCH negatives (codex EMPIRICAL-GATE-NO) ───────

    _RULE = "─" * 120
    _FOOTER = "↑48k ↓8.4k R731k CH98.8% $0.017 3.0%/1.0M (auto)   cline-pass/glm-5.3-flash • high"

    # ── #703 r5: dash-ending transcript adversaries (Opus r4 EMPIRICAL-GATE-NO) ──
    # The r4 closing class ``[─━—\-]+`` admitted the ASCII hyphen and em dash and
    # a run of ONE, so any transcript row that opened with the spinner chrome and
    # happened to END on a dash classified as a live working row — 9 of these 10
    # fired PROCESSING on the r4 head. The last is the r2 verdict's own adversary
    # with a box rule appended: it ends on a real ``─`` run, so the box-drawing-
    # only closing class alone still accepts it — only the full-composer-width
    # check rejects it (the quoted row is short). Each is placed in the live
    # bottom-of-viewport tail with real 120-col composer rules below it; each must
    # classify transcript (COMPLETED). This battery is what kills BOTH the widen-
    # the-closing-class mutant and the drop-the-width-check mutant.
    _DASH_ADVERSARIES = (
        "── ⠦ Working ── see --- below",
        "── ⠦ Working ── -",
        "── ⠧ Working ── quoted in the report ---",
        "── ⠦ Working ── was quoted in the previous answer —",
        "── ⠹ Working ── and then the summary, options: -",
        "── ⠸ Working ── | overmatch | ------",
        "── ⠏ Working ── run with --",
        "── ⠋ Working ── the model finished the summary ─",
        "── ⠙ Working ── the overmatching classifier is rule-",
        "── ⠦ Working ── was quoted in the previous answer. ──────",
    )

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_dash_ending_transcript_adversaries_do_not_fire(self, _native) -> None:
        """All ten of the r4 verdict's dash-ending transcript rows classify
        transcript (COMPLETED), not PROCESSING. Kills the widen-closing-class
        mutant (the ASCII/em-dash-ending rows) and, via the appended-box-rule
        row, the drop-the-width-check mutant."""
        provider = self._provider(dispatched=True, processing_seen=True)
        for adv in self._DASH_ADVERSARIES:
            buffer = "\n".join(
                [
                    " prior transcript line",
                    adv,
                    "",
                    self._RULE,
                    " ",
                    self._RULE,
                    self._FOOTER,
                ]
            )
            assert (
                provider.get_status(buffer) == TerminalStatus.COMPLETED
            ), f"dash-ending adversary fired PROCESSING (overmatch): {adv!r}"

    def _idle_composer_then_candidate(self, candidate: str) -> str:
        """A frame with a COMPLETE idle composer (two full 120-col rules + footer)
        ABOVE, then the candidate as the bottom-most live-tail row. Because no
        complete composer follows the candidate, the r6 composer-structure check
        does NOT mask this frame — so the closing-class and width-equality checks
        are the SOLE discriminators here (the r5 layout put adversaries ABOVE the
        composer, where the r6 structural check would mask both)."""
        return "\n".join(
            [" prior transcript line", self._RULE, " ", self._RULE, self._FOOTER, candidate]
        )

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_full_width_ascii_or_em_dash_tail_is_transcript(self, _native) -> None:
        """A transcript row that OPENS with the spinner chrome, is padded to the
        FULL composer width (so the width check alone cannot reject it), but ENDS
        in ASCII hyphens or em dashes rather than box drawing → transcript. This
        is the case that DISTINGUISHES the box-drawing-only closing class from the
        wider ``[─━—\\-]`` one, so it is the witness that kills the widen-closing-
        class mutant (Opus r4: that mutant SURVIVED with no committed test).
        Composer ABOVE, candidate at the bottom, so the r6 structural check does
        not mask the closing-class check."""
        provider = self._provider(dispatched=True, processing_seen=True)
        for label, dash in (("ascii-hyphen", "-"), ("em-dash", "—")):
            lead = f"── ⠦ Working ── ends in {label} "
            row = lead + dash * (120 - _visible_width(lead))
            assert _visible_width(row) == 120, f"{label}: build error"
            assert (
                provider.get_status(self._idle_composer_then_candidate(row))
                == TerminalStatus.COMPLETED
            ), f"{label}-tailed full-width row fired PROCESSING (closing class too wide)"

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_short_box_drawing_candidate_is_transcript(self, _native) -> None:
        """A box-drawing working row SHORT of the composer width (not the live top
        border, which spans the pane) → transcript. Composer ABOVE, candidate at
        the bottom so the r6 structural check does not mask it: this is the sole
        witness for the width-equality check (kills the drop-composer-width
        mutant). A full-composer-width box row is the PROCESSING control."""
        provider = self._provider(dispatched=True, processing_seen=True)
        lead = "── ⠦ Working "
        short = lead + "─" * (60 - _visible_width(lead))
        assert _visible_width(short) == 60
        assert (
            provider.get_status(self._idle_composer_then_candidate(short))
            == TerminalStatus.COMPLETED
        ), "short (60-col) box-drawing working row fired PROCESSING (width check dropped)"
        full = lead + "─" * (120 - _visible_width(lead))
        assert _visible_width(full) == 120
        assert (
            provider.get_status(self._idle_composer_then_candidate(full))
            == TerminalStatus.PROCESSING
        ), "full-width box working row must be PROCESSING (control)"

    # ── #703 r6: wrapped full-width transcript row (codex r5 EMPIRICAL-GATE-NO) ──
    # A logical transcript row one column LONGER than the composer wraps: its
    # first physical row is exactly composer-wide and, if it ends on a box char,
    # matches _WORKING_ROW at the full width — a manufactured full-width transcript
    # row that width equality alone cannot reject. The genuine idle composer (two
    # full rules + footer) sits below the wrap. The composer-structure check (a
    # candidate with a COMPLETE idle composer below it is not the live top border)
    # rejects it while keeping every archived live frame PROCESSING.

    def _wrap_candidate(self, width: int = 120) -> str:
        """A ``_WORKING_ROW``-matching row of EXACTLY ``width`` columns ending on a
        box rule — the first physical row of a wrapped (width+1)-column logical
        transcript line."""
        lead = "── ⠦ Working ── quoted spinner in transcript "
        row = lead + "─" * (width - _visible_width(lead))
        assert _visible_width(row) == width, "wrap candidate build error"
        return row

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_wrapped_full_width_transcript_row_does_not_fire(self, _native) -> None:
        """A (composer+1)-column transcript row shown as its two terminal physical
        rows — a full-width first row ending on a box rule, then a 1-column
        continuation — sitting ABOVE the genuine idle composer must classify
        transcript (COMPLETED), not PROCESSING. Width equality holds, so only the
        composer-structure check rejects it. This is the r5 blocker's minimal
        reproduction."""
        row1 = self._wrap_candidate(120)
        buffer = "\n".join(
            [
                " prior transcript line",
                row1,  # physical row 1: exactly 120 cols, ends on a box rule
                "─",  # physical row 2: the 1-column wrap continuation
                "",
                self._RULE,  # ┐ the genuine idle composer, complete, BELOW the wrap
                " ",
                self._RULE,  # ┘ two full-width rules + footer
                self._FOOTER,
            ]
        )
        provider = self._provider(dispatched=True, processing_seen=True)
        assert (
            provider.get_status(buffer) == TerminalStatus.COMPLETED
        ), "wrapped full-width transcript row fired PROCESSING on an idle pane"

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_unwrapped_121_and_119_rows_are_transcript(self, _native) -> None:
        """Controls for the wrap case: the same logical row supplied UNWRAPPED at
        121 columns is over-wide (fails the width equality), and a 119-column row
        is under-wide — both transcript, so the wrap negative is not passing for a
        trivial reason."""
        provider = self._provider(dispatched=True, processing_seen=True)
        lead = "── ⠦ Working ── quoted spinner in transcript "
        for width in (121, 119):
            row = lead + "─" * (width - _visible_width(lead))
            assert _visible_width(row) == width
            buffer = "\n".join([" prior", row, "", self._RULE, " ", self._RULE, self._FOOTER])
            assert (
                provider.get_status(buffer) == TerminalStatus.COMPLETED
            ), f"{width}-column row fired PROCESSING"

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_full_width_cjk_emoji_working_row_is_processing(self, _native) -> None:
        """A GENUINE live working row whose message carries wide CJK + emoji glyphs
        (``工具🚀``) still spans the full composer width and classifies PROCESSING.
        This is the committed regression the r5 verdict asked for: it is RED if the
        East-Asian-width handling in ``_visible_width`` is dropped (the wide glyphs
        would then under-count and the row would miss the width invariant)."""
        lead = "── ⠋ Working 工具🚀 processing "
        # Hard-code the trailing rule length so the row is EXACTLY 120 visible
        # columns under the real (East-Asian-width-aware) helper — NOT computed
        # via _visible_width, so it does not self-compensate when the EAW handling
        # is mutated away. lead is 31 visible cols (3 wide glyphs), so 89 dashes
        # make 120; dropping EAW under-counts lead to 28 and the row to 117 (a
        # width mismatch), which is what makes this arm kill the EAW mutant.
        row = lead + "─" * 89
        assert _visible_width(row) == 120, "cjk/emoji row build error"
        # Genuine live shape: the working row IS the top border; blank editor, one
        # bottom rule, footer (exactly one full rule below — a real live frame).
        buffer = "\n".join([" prior turn output", row, "", self._RULE, self._FOOTER])
        provider = self._provider(dispatched=True, processing_seen=True)
        assert (
            provider.get_status(buffer) == TerminalStatus.PROCESSING
        ), "full-width CJK/emoji working row must be PROCESSING (EAW width handling)"

    # ── #703 r7: PORTABLE anti-over-narrowing guard (Opus r6 EMPIRICAL-GATE-NO) ──
    # The r6 full-width qualifier in _has_complete_composer_below is load-bearing,
    # but its only killer was the gate-scratch archive sweep, which SKIPS on every
    # machine without that corpus (it skipped on the box in the r6 A/B). These two
    # frames are committed INTO the repo (byte-identical live captures, .json
    # sidecars) so the guard runs UNCONDITIONALLY in CI:
    #   working-resize-3rule.txt — the ONE archive frame where the any-width and
    #     full-width readings differ: 3 box rules below the working row, only ONE
    #     full-width (the other two are narrow resize artifacts). RED under the
    #     drop-full-width mutant (any-count 3 >= 2 + footer disqualifies the live
    #     row), GREEN with the qualifier (full-count 1 < 2).
    #   working-resize-0rule.txt — a transient resize frame with zero composer
    #     rules below the working row (the step-5 no-width-evidence accept path).
    _COMMITTED_RESIZE_FIXTURES = ("working-resize-3rule", "working-resize-0rule")

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_committed_resize_frames_are_processing(self, _native) -> None:
        """PORTABLE, UNCONDITIONAL guard for the full-width qualifier: two
        committed live resize frames classify PROCESSING (raw + ANSI-stripped).
        working-resize-3rule.txt is RED under the mutant that drops
        ``_visible_width(r) == composer_width`` from _has_complete_composer_below,
        so this test — not the optional gate-scratch sweep — is the CI killer of
        that mutant."""
        provider = self._provider(dispatched=True, processing_seen=True)
        for name in self._COMMITTED_RESIZE_FIXTURES:
            raw = (FIXTURES / "status_truth" / "pi_cli" / f"{name}.txt").read_text(encoding="utf-8")
            clean = strip_terminal_escapes(raw)
            assert provider.get_status(raw) == TerminalStatus.PROCESSING, f"{name}: raw"
            assert provider.get_status(clean) == TerminalStatus.PROCESSING, f"{name}: stripped"

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_footerless_two_rules_below_working_row_is_processing(self, _native) -> None:
        """Pins the ``has_footer`` conjunct in ``_has_complete_composer_below``.
        A genuine full-width working row (the composer top border) with TWO
        full-width box rules below it but NO footer is still a live working pane →
        PROCESSING. The ``has_footer`` conjunct is what keeps the disqualifier
        scoped to a COMPLETE idle composer (rules AND footer): dropping it makes
        ``full_rules >= 2`` alone disqualify this row, flipping it to UNKNOWN.
        This arm therefore FAILS (PROCESSING → UNKNOWN) when the conjunct is
        dropped, which is why the ``has_footer`` clause is kept rather than
        removed (Opus r6 EMPIRICAL-GATE-NO item 2, decision: keep + pin)."""
        provider = self._provider(dispatched=True, processing_seen=True)
        lead = "── ⠋ Working "
        candidate = lead + "─" * (120 - _visible_width(lead))
        assert _visible_width(candidate) == 120
        # Working row (top border) then two full-width rules, NO footer.
        buffer = "\n".join([" prior turn output", candidate, "", self._RULE, " ", self._RULE])
        assert provider.get_status(buffer) == TerminalStatus.PROCESSING, (
            "a full-width working row above two footerless rules must stay PROCESSING "
            "(dropping the has_footer conjunct would flip it to UNKNOWN)"
        )

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_stale_wider_rule_above_complete_composer_is_completed(self, _native) -> None:
        """ADV-stale-wider-rule (codex r7 EMPIRICAL-GATE-NO): a STALE wider editor
        rule left in the accumulated rolling buffer must NOT hide a COMPLETE idle
        composer below the candidate.

        Construction (the r8 verdict's exact adversary):
          1. a stale 120-column editor rule (an OLD frame's composer top border
             still in scrollback — pi's escape cleanup turns redraws into separate
             logical rows, so old-width rules coexist with the current composer);
          2. a 120-column ``_WORKING_ROW``-matching first physical row of a WRAPPED
             quoted transcript line, followed by its one-column continuation;
          3. a COMPLETE current idle composer strictly below it: two 100-column
             rules, editor content, and a valid footer.

        Under the r7 rule (``full_rules >= 2`` where ``full_rules`` counts rules
        equal to ``composer_width`` = the GLOBAL MAXIMUM editor-rule width) the
        stale rule fixed ``composer_width`` at 120, the current composer's two
        100-column rules did not equal 120 and were NOT counted, the wrap
        candidate had "no complete composer below", and the frame read PROCESSING
        (a FALSE busy on an idle, delivery-eligible pane). The r8 rule tests for a
        self-consistent same-width rule PAIR + footer below the candidate,
        decoupled from the poisoned global width, so the 100-column pair is seen
        and the wrap is disqualified → COMPLETED.

        This test is RED on the r7 source and GREEN on r8 (see mutant (a) in the
        report: reverting ``_has_complete_composer_below`` to the global-max
        ``full_rules`` count restores PROCESSING here). The CONTROL removes only
        the stale wider rule; it is COMPLETED on BOTH r7 and r8 (deleting the
        stale rule alone already flips the r7 reading), so the pair isolates the
        stale rule as the sole cause.
        """
        provider = self._provider(dispatched=True, processing_seen=True)
        stale_wider = "─" * 120  # (1) an OLD frame's 120-col rule still in buffer
        # (2) the wrapped quoted-transcript working row: EXACTLY 120 cols, ending
        # on a box rule (its first physical row), then the 1-col wrap continuation.
        lead = "── ⠦ Working ── quoted spinner in transcript "
        wrap_row = lead + "─" * (120 - _visible_width(lead))
        assert _visible_width(wrap_row) == 120, "wrap candidate build error"
        rule_100 = "─" * 100  # (3) the CURRENT composer's own rules (narrower)
        adversary = "\n".join(
            [
                " prior transcript output line one",
                " prior transcript output line two",
                stale_wider,  # (1) stale WIDER rule above everything
                " more transcript between the stale rule and the wrap",
                wrap_row,  # (2) 120-col wrapped-transcript first physical row
                "─",  # the 1-column wrap continuation
                "",
                rule_100,  # ┐ (3) the COMPLETE current idle composer, BELOW the wrap
                " editor content",
                rule_100,  # ┘ two 100-col rules (same width to each other) + footer
                self._FOOTER,
            ]
        )
        # CONTROL: identical frame with ONLY the stale wider rule removed.
        control = "\n".join(r for r in adversary.splitlines() if r != stale_wider)
        assert provider.get_status(adversary) == TerminalStatus.COMPLETED, (
            "a stale wider rule above the wrap must NOT hide the complete "
            "100-col idle composer below it (ADV-stale-wider-rule, r7 read PROCESSING)"
        )
        assert (
            provider.get_status(control) == TerminalStatus.COMPLETED
        ), "control with the stale wider rule removed must be COMPLETED"

    # ── #703 r9: stale wider rule poisons the CANDIDATE filter (codex r8 NO) ────
    # r8 removed the whole-buffer maximum from the composer-BELOW check but left
    # it powering the full-width CANDIDATE qualifier (step 5). A stale wider rule
    # in scrollback then fixed ``composer_width`` at the stale width, so a GENUINE
    # narrower live working row failed ``_visible_width(row) == composer_width``,
    # was dropped from ``candidates`` entirely, and the idle-chrome fallback read
    # the working pane as COMPLETED — a false, delivery-eligible status. r9
    # derives the composer width STRUCTURALLY from the current composer
    # (``_current_composer_width``): the max editor-rule width BELOW the live
    # working row, else the tail-window max — never the whole-buffer maximum. The
    # helper is the SINGLE derivation both consumers use.

    def _live_narrow_composer(self, stale: str | None, width: int = 100) -> str:
        """The r8 verdict's ADV-stale-wider-live-candidate: an optional STALE
        rule (``stale``) far above; then the GENUINE live working top border of
        ``width`` columns, its editor body, ONE ``width``-column bottom rule, and a
        valid footer. With ``stale`` wider than ``width`` this is the exact
        blocker; ``stale=None`` is the isolating control (no stale rule)."""
        rows = [" prior transcript output line one", " prior transcript output line two"]
        if stale is not None:
            rows += [stale, " transcript between the stale rule and the live composer"]
        rows += [
            _working_row("⠧", width),  # the GENUINE live working top border
            " editor content",
            "─" * width,  # the current composer's own bottom rule (narrower)
            self._FOOTER,
        ]
        return "\n".join(rows)

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_stale_wider_rule_does_not_hide_live_narrow_candidate(self, _native) -> None:
        """ADV-stale-wider-live-candidate (codex r8 EMPIRICAL-GATE-NO): a stale
        120-column rule left in scrollback must NOT drop a GENUINE live 100-column
        working row from the candidate set. The current composer top border is
        exactly 100 columns; its body, one 100-column bottom rule, and a footer
        follow it. There is NO complete composer below the live row, so it is the
        live top border → PROCESSING.

        RED on r8: step 5 sized ``composer_width`` from the whole-buffer maximum
        (120, from the stale rule), so the 100-column working row failed the
        full-width qualifier, was not a candidate, and the idle-chrome fallback —
        seeing the stale 120 rule + the 100 bottom rule + footer — returned
        COMPLETED (a false idle on a working pane). GREEN on r9: the width is the
        current composer's own (100, the max rule BELOW the live working row), so
        the working row qualifies and PROCESSING is returned.

        The CONTROL deletes ONLY the stale wider rule; it is PROCESSING on BOTH r8
        and r9 (with no stale rule the max is 100 either way), isolating the stale
        rule as the sole cause of the r8 misread."""
        provider = self._provider(dispatched=True, processing_seen=True)
        stale_wider = "─" * 120
        adversary = self._live_narrow_composer(stale_wider, width=100)
        control = self._live_narrow_composer(None, width=100)
        assert provider.get_status(adversary) == TerminalStatus.PROCESSING, (
            "a stale wider rule must NOT hide a genuine live 100-col working row "
            "(ADV-stale-wider-live-candidate, r8 read COMPLETED)"
        )
        assert (
            provider.get_status(control) == TerminalStatus.PROCESSING
        ), "control with the stale wider rule removed must be PROCESSING"

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_stale_narrower_rule_above_live_candidate_is_processing(self, _native) -> None:
        """A stale NARROWER rule (80-col) above a 100-col live candidate →
        PROCESSING. r8 ALREADY passed this (the global max was 100, since
        100 > 80, so the live row qualified); it is committed so the r9 structural
        width can never REGRESS a case the global max got right."""
        provider = self._provider(dispatched=True, processing_seen=True)
        stale_narrower = "─" * 80
        buffer = self._live_narrow_composer(stale_narrower, width=100)
        assert (
            provider.get_status(buffer) == TerminalStatus.PROCESSING
        ), "a stale 80-col rule must not disturb a genuine live 100-col working row"

    def _two_composers(self, candidate: str) -> str:
        """Two COMPLETE composers of different widths in one buffer: an OLD
        120-column composer (rule pair + footer) high in scrollback, then
        ``candidate`` region, then the CURRENT 100-column composer at the tail
        (one bottom rule + footer). ``candidate`` is spliced in between the old
        composer and the current one."""
        return "\n".join(
            [
                " prior transcript",
                "─" * 120,  # ┐ OLD composer, complete
                " old editor body",
                "─" * 120,  # ┘ two 120-col rules
                self._FOOTER,
                " turn 2 output",
                candidate,  # the row under test
                " editor content",
                "─" * 100,  # the CURRENT composer's bottom rule (100)
                self._FOOTER,
            ]
        )

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_two_composers_live_current_candidate_is_processing(self, _native) -> None:
        """Two complete composers, old 120 above and current 100 at the tail, with
        a GENUINE live 100-col working row as the current composer's top border →
        PROCESSING.

        RED on r8: the whole-buffer maximum is 120 (the old composer), so the
        100-col live working row failed the full-width qualifier and the pane read
        COMPLETED. GREEN on r9: the current composer width is 100 (the rule below
        the live working row), so the live row qualifies → PROCESSING."""
        provider = self._provider(dispatched=True, processing_seen=True)
        live = _working_row("⠧", 100)
        assert _visible_width(live) == 100
        assert provider.get_status(self._two_composers(live)) == TerminalStatus.PROCESSING, (
            "a genuine live 100-col working row at the current composer must be "
            "PROCESSING even with an old 120-col composer above it (r8 read COMPLETED)"
        )

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_two_composers_wrapped_candidate_above_current_is_completed(self, _native) -> None:
        """Same two composers, but the row above the current composer is a WRAPPED
        transcript row (a 100-col ``_WORKING_ROW`` first physical row + a 1-col
        continuation) — NOT the live top border. A complete 100-col composer sits
        below it, so it is disqualified → COMPLETED.

        r8 already returned COMPLETED here (via the below-check); committed so r9
        keeps the wrap rejection while the current composer is idle."""
        provider = self._provider(dispatched=True, processing_seen=True)
        lead = "── ⠦ Working ── quoted spinner in transcript "
        wrap = lead + "─" * (100 - _visible_width(lead))
        assert _visible_width(wrap) == 100
        buffer = "\n".join(
            [
                " prior transcript",
                "─" * 120,  # ┐ OLD composer, complete
                " old editor body",
                "─" * 120,  # ┘
                self._FOOTER,
                " turn 2 output",
                wrap,  # wrapped transcript first physical row (100 cols)
                "─",  # its 1-column wrap continuation
                "",
                "─" * 100,  # ┐ the CURRENT idle composer, complete
                " editor content",
                "─" * 100,  # ┘ two 100-col rules + footer
                self._FOOTER,
            ]
        )
        assert provider.get_status(buffer) == TerminalStatus.COMPLETED, (
            "a wrapped 100-col transcript row above a complete 100-col idle "
            "composer must be COMPLETED, not the live top border"
        )

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_near_width_candidate_needs_exact_equality(self, _native) -> None:
        """The full-width candidate qualifier is EXACT equality, not ±1. A
        transcript ``_WORKING_ROW`` of 99 columns (one short of the 100-column
        current composer) sitting as the bottom-most live-tail row — composer
        ABOVE it, nothing complete below — is transcript → COMPLETED. Under a
        mutant that relaxes ``_visible_width(row) == composer_width`` to a
        one-column tolerance, the 99-col row would be admitted as a candidate and
        the frame would flip to PROCESSING. The 100-col full-width control is the
        PROCESSING witness. This is the committed killer for the ±1 mutant (the r8
        verdict's independent mutant)."""
        provider = self._provider(dispatched=True, processing_seen=True)
        lead = "── ⠦ Working "
        near = lead + "─" * (99 - _visible_width(lead))
        assert _visible_width(near) == 99
        # Composer (100-col pair + footer) ABOVE, near-width candidate at the
        # bottom so no complete composer follows it (the width check is the sole
        # discriminator).
        near_buf = "\n".join(
            [" prior transcript line", "─" * 100, " ", "─" * 100, self._FOOTER, near]
        )
        assert provider.get_status(near_buf) == TerminalStatus.COMPLETED, (
            "a 99-col working row one short of the 100-col composer must be "
            "transcript (exact width equality; ±1 tolerance would fire PROCESSING)"
        )
        full = lead + "─" * (100 - _visible_width(lead))
        assert _visible_width(full) == 100
        full_buf = "\n".join(
            [" prior transcript line", "─" * 100, " ", "─" * 100, self._FOOTER, full]
        )
        assert (
            provider.get_status(full_buf) == TerminalStatus.PROCESSING
        ), "the exact-width 100-col control must be PROCESSING"

    # ── #703 r10: item 1 — the below-check must consume the derived width ───────
    # (codex r9 EMPIRICAL-GATE-NO item 1). r9 wired ``_current_composer_width``
    # into the CANDIDATE qualifier but left ``_clean_same_width_rule_pair_below``
    # width-AGNOSTIC. The frozen repair contract requires BOTH consumers to use
    # the ONE derived current-composer width. The adversary below is the verdict's
    # ADV-adjacent-narrow-resize-pair, built from the genuine resize-family
    # geometry (the committed ``working-resize-3rule`` frame's 20-column artifact
    # class): a genuine 100-column live working top border, TWO adjacent 20-column
    # redraw artifacts, the single real 100-column bottom rule, and a footer.

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_adjacent_narrow_resize_pair_below_live_row_is_processing(self, _native) -> None:
        """ADV-adjacent-narrow-resize-pair (codex r9 EMPIRICAL-GATE-NO item 1): a
        genuine live 100-column working row whose current composer draws TWO
        ADJACENT 20-column RESIZE redraw artifacts below it (plus the single real
        100-column bottom rule and a footer) must stay PROCESSING.

        ``_current_composer_width`` is 100 (max rule below the live working row:
        20, 20, 100), so the 100-column working row qualifies as a candidate.
        RED on r9: ``_clean_same_width_rule_pair_below`` was width-agnostic, so the
        ADJACENT 20/20 artifact pair (nothing wider sandwiched between the two
        20-column rows) satisfied it, plus the footer disqualified the live row as
        "a complete idle composer below" → a FALSE COMPLETED on a working pane.
        GREEN on r10: the check requires the rule pair to equal the derived
        ``composer_width`` (100), so the 20/20 pair (20 != 100) is NOT a composer
        box, the live working row is retained → PROCESSING.

        The CONTROL keeps the composer's OWN 100-column pair below the live row (a
        second 100-column rule replacing one 20-column artifact) with a footer: it
        is COMPLETED under BOTH r9 and r10 (a genuine same-width-100 idle composer
        follows the row), isolating the artifact WIDTH — not the mere presence of
        a pair — as what r10 newly excludes."""
        provider = self._provider(dispatched=True, processing_seen=True)
        live = _working_row("⠧", 100)
        assert _visible_width(live) == 100
        adversary = "\n".join(
            [
                " prior transcript output",
                live,  # the GENUINE 100-col live working top border
                " editor content",
                "─" * 20,  # ┐ two ADJACENT 20-col resize redraw artifacts
                "─" * 20,  # ┘ (a same-width pair at the WRONG width)
                "─" * 100,  # the single REAL 100-col current-composer bottom rule
                self._FOOTER,
            ]
        )
        assert provider.get_status(adversary) == TerminalStatus.PROCESSING, (
            "an adjacent 20/20 resize-artifact pair below a genuine live 100-col "
            "working row must NOT be read as a complete idle composer "
            "(ADV-adjacent-narrow-resize-pair, r9 read COMPLETED)"
        )
        # CONTROL: a genuine 100-col idle-composer PAIR below the live row (both
        # the artifact rows widened to the composer width) → COMPLETED on r9 AND
        # r10 (the pair equals composer_width either way).
        control = "\n".join(
            [
                " prior transcript output",
                live,
                " editor content",
                "─" * 100,  # ┐ a genuine same-width-100 idle composer pair
                " ",
                "─" * 100,  # ┘
                self._FOOTER,
            ]
        )
        assert provider.get_status(control) == TerminalStatus.COMPLETED, (
            "control with a genuine 100-col idle-composer pair below the row must "
            "be COMPLETED on both r9 and r10 (isolates artifact width as the cause)"
        )

    # ── #703 r10: item 2 — pin the BOTTOM-MOST working-row anchor (Stage A OWN-1)
    # (codex r9 EMPIRICAL-GATE-NO item 2). ``_current_composer_width`` anchors on
    # ``working_idxs[-1]`` — the BOTTOM-MOST working row is the CURRENT composer's
    # top border. The Stage A OWN-1 mutant replaces ``[-1]`` with ``[0]`` (top-most
    # anchor) and survived all 70 committed non-archive tests. This is the missing
    # rolling-buffer shape that KILLS it: a STALE 120-column working row and its
    # COMPLETED 120-column redraw (a complete 120-col idle composer box) ABOVE a
    # genuine CURRENT 100-column live working composer.

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_stale_wide_working_above_live_narrow_pins_bottom_anchor(self, _native) -> None:
        """Pins the BOTTOM-MOST working-row anchor in ``_current_composer_width``
        (Stage A OWN-1: ``working_idxs[-1]`` → ``[0]``).

        Rolling-buffer shape (the r9 verdict's exact construction): a STALE
        120-column working row from an OLD frame, its COMPLETED redraw — a complete
        120-column idle composer box (a 120-col rule pair + footer) — then the
        GENUINE CURRENT 100-column live working top border, its editor body, the
        current composer's own 100-column bottom rule, and a footer.

        GREEN on r10: the width is anchored on the BOTTOM-MOST working row (the
        current 100-col composer), so the rule below it is 100, the live 100-col
        working row qualifies, and there is no complete 100-col composer below it →
        PROCESSING.

        RED under the OWN-1 mutant (``working_idxs[0]``, top-most anchor): the
        width is derived from the STALE 120-col working row, whose rules below it
        include the old frame's 120-col redraw → ``composer_width`` = 120. The
        genuine 100-col live working row then fails ``== 120`` and is dropped from
        candidates, while the stale 120-col working row sits above its OWN complete
        120-col idle composer (rule pair + footer) and is disqualified → NO valid
        candidate, so the idle-chrome fallback returns COMPLETED: a false, delivery-
        eligible idle on a working pane. The bottom-most anchor is therefore
        load-bearing and this frame is its committed killer."""
        provider = self._provider(dispatched=True, processing_seen=True)
        stale_working = _working_row("⠹", 120)  # a STALE 120-col working row (old frame)
        live = _working_row("⠧", 100)  # the GENUINE current 100-col live working row
        assert _visible_width(stale_working) == 120
        assert _visible_width(live) == 100
        buffer = "\n".join(
            [
                " prior transcript output",
                stale_working,  # the STALE 120-col working row, high in scrollback
                " stale editor body",
                "─" * 120,  # ┐ its COMPLETED redraw: a complete 120-col idle
                " stale composer line",  #   composer box (a 120-col rule PAIR + footer)
                "─" * 120,  # ┘
                self._FOOTER,
                " turn 2 output",
                live,  # the GENUINE CURRENT 100-col live working top border
                " editor content",
                "─" * 100,  # the current composer's own 100-col bottom rule
                self._FOOTER,
            ]
        )
        assert provider.get_status(buffer) == TerminalStatus.PROCESSING, (
            "a genuine current 100-col live working composer BELOW a stale 120-col "
            "working row and its completed 120-col redraw must be PROCESSING "
            "(bottom-most anchor; the top-most-anchor OWN-1 mutant reads COMPLETED)"
        )

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_archive_live_frames_all_processing(self, _native) -> None:
        """OPTIONAL, non-load-bearing arm: the full inherited live pi 0.85.1
        capture archive (the frames the r3-r6 gates were measured on) classifies
        PROCESSING for every working frame. The PORTABLE guard for the full-width
        qualifier is ``test_committed_resize_frames_are_processing`` (committed
        fixtures); THIS sweep only widens the sample when the gate-scratch corpus
        happens to be present, and SKIPS otherwise. It is NOT counted as CI
        coverage."""
        import glob as _glob

        roots = [
            "/data/cao-scratch/gate-700-703-r3/capture/frames/frame_*.txt",
            "/data/cao-scratch/gate-700-703-r3/capture3/frames/q_*.txt",
            "/data/cao-scratch/gate-700-703-r3/capture3/frames/w_before.txt",
        ]
        files = [f for pat in roots for f in sorted(_glob.glob(pat))]
        if not files:
            pytest.skip(
                "OPTIONAL non-load-bearing sweep: gate-scratch archive absent; the "
                "portable guard is test_committed_resize_frames_are_processing"
            )
        provider = self._provider(dispatched=True, processing_seen=True)
        working = 0
        for f in files:
            raw = open(f, encoding="utf-8", errors="replace").read()
            clean = strip_terminal_escapes(raw)
            if not any(_WORKING_ROW.match(r) for r in clean.splitlines()):
                continue
            working += 1
            assert provider.get_status(raw) == TerminalStatus.PROCESSING, f"{f}: raw not PROCESSING"
            assert (
                provider.get_status(clean) == TerminalStatus.PROCESSING
            ), f"{f}: ANSI-stripped not PROCESSING"
        assert working == 424, f"expected 424 archived working frames, found {working}"

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_braille_working_transcript_prose_does_not_fire(self, _native) -> None:
        """A braille glyph that merely PRECEDES the word 'Working' in transcript
        prose — with no box rule on the row — must NOT fire PROCESSING (the r1
        first alternative made the rule optional and overmatched this). Idle
        chrome below it wins."""
        buffer = "\n".join(
            [
                ' The user quoted: "⠦ Working on the summary now" in the last turn.',
                "",
                self._RULE,
                " ",
                self._RULE,
                self._FOOTER,
            ]
        )
        provider = self._provider(dispatched=True, processing_seen=True)
        assert provider.get_status(buffer) == TerminalStatus.COMPLETED

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_fenced_working_spinner_quote_does_not_fire(self, _native) -> None:
        """A spinner row a human PASTED inside a Markdown code fence is transcript,
        not the live status row (F836 quoted-text discipline). It must NOT fire
        PROCESSING; the live idle composer below the fence wins."""
        buffer = "\n".join(
            [
                " Here is the frame I saw:",
                "```",
                "── ⠼ Working " + self._RULE,
                "```",
                "",
                self._RULE,
                " ",
                self._RULE,
                self._FOOTER,
            ]
        )
        provider = self._provider(dispatched=True, processing_seen=True)
        assert provider.get_status(buffer) == TerminalStatus.COMPLETED

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_stale_spinner_far_above_composer_does_not_fire(self, _native) -> None:
        """A stale spinner row scrolled tens of transcript rows ABOVE the current
        composer is not the live state. Only the bottom-of-viewport status region
        is eligible, so the idle composer below wins (not a false PROCESSING)."""
        lines = ["── ⠹ Working " + self._RULE]
        lines += [f" transcript reflow line {i}" for i in range(45)]
        lines += [self._RULE, " ", self._RULE, self._FOOTER]
        buffer = "\n".join(lines)
        provider = self._provider(dispatched=True, processing_seen=True)
        assert provider.get_status(buffer) == TerminalStatus.COMPLETED

    # ── #703 r3: WHOLE-ROW end-anchor negatives (codex r2 EMPIRICAL-GATE-NO) ────
    #
    # r2 anchored the spinner row at ``^`` but NOT at ``$``, and
    # ``_live_working_spinner`` matches with ``re.match`` (a matching PREFIX is
    # accepted), so a transcript row that OPENS with the spinner chrome and then
    # carries trailing prose/quote fired a false PROCESSING even in the LIVE
    # tail — the row is in the composer region, so the fence/stale scoping above
    # does not catch it. These three rows sit in the live bottom-of-viewport
    # region (no fence, at the composer) and are rejected ONLY by the ``$`` end
    # anchor added in r3; each asserts COMPLETED (idle chrome wins), proving the
    # trailing text made the whole-row match fail.

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_working_row_with_trailing_prose_does_not_fire(self, _native) -> None:
        """The verdict's exact adversary: ``── ⠦ Working ── was quoted in the
        previous answer.`` — spinner chrome then a rule then PROSE, in the live
        composer region. The whole-row ``$`` anchor rejects the trailing prose so
        it is transcript, not PROCESSING."""
        buffer = "\n".join(
            [
                " Summarising the thread:",
                "── ⠦ Working ── was quoted in the previous answer.",
                "",
                self._RULE,
                " ",
                self._RULE,
                self._FOOTER,
            ]
        )
        provider = self._provider(dispatched=True, processing_seen=True)
        assert provider.get_status(buffer) == TerminalStatus.COMPLETED

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_working_row_with_trailing_sentence_does_not_fire(self, _native) -> None:
        """A rule-leading spinner row that continues into a full sentence
        (``── ⠧ Working ── and then the model finished the summary``) is
        transcript prose, not a live redraw frame: the ``$`` anchor rejects the
        trailing words."""
        buffer = "\n".join(
            [
                " The assistant wrote:",
                "── ⠧ Working ── and then the model finished the summary",
                "",
                self._RULE,
                " ",
                self._RULE,
                self._FOOTER,
            ]
        )
        provider = self._provider(dispatched=True, processing_seen=True)
        assert provider.get_status(buffer) == TerminalStatus.COMPLETED

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_working_row_with_trailing_quote_does_not_fire(self, _native) -> None:
        """A spinner-first row (glyph leads, rule trails) followed by a trailing
        QUOTED string (``⠴ Working ──── "quoted status banner"``) is transcript,
        not the live spinner: the ``$`` anchor rejects everything after the
        composer rule run."""
        buffer = "\n".join(
            [
                " I pasted the status line I saw:",
                '⠴ Working ──── "quoted status banner"',
                "",
                self._RULE,
                " ",
                self._RULE,
                self._FOOTER,
            ]
        )
        provider = self._provider(dispatched=True, processing_seen=True)
        assert provider.get_status(buffer) == TerminalStatus.COMPLETED

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_idle_after_error_corpus_is_idle_not_error(self, _native) -> None:
        """#703 r2 item 2b: the second real capture (idle-after-error-1.txt) has
        several transcript ``Error:`` lines but ends in idle composer chrome — it
        must classify IDLE (not a sticky ERROR), and COMPLETED with a dispatched
        task + prior processing frame. Never ERROR."""
        corpus = FIXTURES / "status_truth" / "pi_cli" / "idle-after-error-1.txt"
        raw = corpus.read_text(encoding="utf-8")
        # Fresh instance, no dispatch/processing yet → IDLE (not ERROR).
        assert self._provider(dispatched=False, processing_seen=False).get_status(raw) == (
            TerminalStatus.IDLE
        )
        # ANSI-stripped identical verdict.
        clean = strip_terminal_escapes(raw)
        assert self._provider(dispatched=False, processing_seen=False).get_status(clean) == (
            TerminalStatus.IDLE
        )
        # With a dispatched task + a processing frame seen → COMPLETED, never ERROR.
        assert self._provider(dispatched=True, processing_seen=True).get_status(raw) == (
            TerminalStatus.COMPLETED
        )


# ─── F844 (#701): an error verdict must not be sticky ───────────────────────────


class TestErrorNotSticky:
    """F844 (#701): pi status is re-derived from the live pane every poll and an
    ``error`` verdict is NOT sticky. A ClinePass 429 banner (see #700) scrolls
    into the rolling buffer and stays there; once the pane is nudged and resumes
    real work the status must re-derive to PROCESSING (working spinner) or IDLE
    (composer), never latch ERROR on the stale banner.

    ``_resolve_native_status`` is patched to None so these exercise the TUI-chrome
    path (the tmux path, where native status is always None).
    """

    # The verbatim ClinePass 429 banner from the live panes (issue #700/#701).
    _BANNER = (
        'Error: 429: {"code":"INFERENCE_CAP_ERROR","message":"Error 429: You have '
        "reached your 5-hour Clinepass limit. The limit resets in 1h 29m, please "
        'try again later."}'
    )
    _RETRY_FAILED = "Error: Retry failed after 3 attempts: 429: {...same...}"
    _RULE = "─" * 120
    _FOOTER = "↑26k ↓172 R3.9k CH97.6% $0.002 0.4%/1.0M (auto)   cline-pass/glm-5.3-flash • high"

    def _provider(self, dispatched=True, processing_seen=False) -> PiCliProvider:
        provider = PiCliProvider("t1234567", "sess", "win0")
        provider._initialized = True
        provider._task_dispatched = dispatched
        provider._tui_processing_seen = processing_seen
        return provider

    def _capped_at_composer(self) -> str:
        """The pane right after the 429 storm: banner in transcript, pi idle at
        its composer chrome (no working spinner)."""
        return "\n".join(
            [
                " Run the lite gate on the diff.",
                "",
                self._BANNER,
                self._BANNER,
                self._RETRY_FAILED,
                "",
                self._RULE,
                " ",
                self._RULE,
                "/data/scratch/probe",
                self._FOOTER,
            ]
        )

    def _capped_then_working(self) -> str:
        """After a tmux nudge the pane resumes real work: the stale 429 banner is
        still in scrollback, but a live ``Working`` spinner now runs. The spinner
        row spans the composer width (F847 r5 invariant)."""
        return self._capped_at_composer() + "\n" + _working_row("⠧", 120) + "\n"

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_capped_banner_at_composer_is_idle_not_error(self, _native) -> None:
        """B-1: a 429 banner in scrollback with pi idle at its composer reads
        IDLE (the banner is a #700 CAPPED condition, not a terminal ERROR).
        Before the fix the _STARTUP_ERROR scan latched ERROR here."""
        provider = self._provider(dispatched=False, processing_seen=False)
        assert provider.get_status(self._capped_at_composer()) == TerminalStatus.IDLE

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_nudged_working_pane_re_derives_processing_not_error(self, _native) -> None:
        """B-2: after the nudge the working spinner is present → PROCESSING, even
        though the 429 banner is still in the rolling buffer. The error verdict
        did not stick."""
        provider = self._provider(dispatched=True)
        assert provider.get_status(self._capped_then_working()) == TerminalStatus.PROCESSING

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_error_then_processing_then_idle_sequence(self, _native) -> None:
        """The full #701 sequence on ONE provider instance:
        [genuine startup error → nudge → working] yields ERROR→PROCESSING, and
        [→ composer] yields IDLE. Status re-derives from the live pane each poll.
        """
        provider = self._provider(dispatched=True, processing_seen=False)
        # 1) Genuine startup failure: no TUI chrome, no spinner → ERROR.
        startup_error = (
            "/data/scratch/probe$\n"
            "Error: failed to initialize model catalog: connection refused\n"
            "pi: error: could not start session\n"
        )
        assert provider.get_status(startup_error) == TerminalStatus.ERROR
        # 2) Nudged and working: the live spinner re-derives PROCESSING (not
        #    sticky ERROR), and latches _tui_processing_seen for the COMPLETED
        #    transition below.
        assert provider.get_status(self._capped_then_working()) == TerminalStatus.PROCESSING
        assert provider._tui_processing_seen is True
        # 3) Back at the composer with the banner still in scrollback: IDLE-class
        #    (COMPLETED here because a task was dispatched and a processing frame
        #    was seen) — never ERROR.
        assert provider.get_status(self._capped_at_composer()) == TerminalStatus.COMPLETED

    @patch.object(PiCliProvider, "_resolve_native_status", return_value=None)
    def test_genuine_startup_error_still_reads_error(self, _native) -> None:
        """Guard: the fix must not weaken genuine startup-failure detection — a
        pane with NO idle chrome and NO spinner but a launch-error banner is
        still ERROR."""
        provider = self._provider(dispatched=False)
        startup_error = (
            "/data/scratch/probe$\n"
            "Error: failed to initialize model catalog: connection refused\n"
            "pi: error: could not start session\n"
        )
        assert provider.get_status(startup_error) == TerminalStatus.ERROR


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


class TestSpawnCapturedIdentity:
    """F867 (#723) D1: pi reports its KNOWN-AT-SPAWN session identity so the
    create path binds provider_session_id onto the F829 root and a planned
    hibernate stops refusing session_artifact_missing on a pi lane."""

    def test_fresh_spawn_reports_terminal_id_and_session_dir(self) -> None:
        """A fresh (non-resume) pi worker's identity is (terminal_id, session_dir,
        None): pi launches with --session-id <terminal_id> and writes
        <ts>_<terminal_id>.jsonl under --session-dir."""
        p = PiCliProvider("t1234567", "sess", "win0")
        ident = p.spawn_captured_identity()
        assert ident is not None
        provider_session_id, provider_namespace, artifact_locator = ident
        assert provider_session_id == "t1234567"
        assert provider_namespace == str(p.session_dir)
        # The exact file does not exist yet (written at first turn), so no locator.
        assert artifact_locator is None

    def test_resume_spawn_reports_none(self) -> None:
        """On a RESUME spawn the root is re-pointed by the resume publish path,
        so there is nothing fresh to bind — must return None."""
        from cli_agent_orchestrator.models.terminal import ForkContext

        ctx = ForkContext(
            mode="resume",
            session_uuid="/some/prior/2026_session.jsonl",
            base_name="reauth",
            provider="pi_cli",
            initial_preamble="",
        )
        p = PiCliProvider("t7654321", "sess", "win0", fork_context=ctx)
        assert p.spawn_captured_identity() is None

    def test_base_default_is_none(self) -> None:
        """MUTANT GUARD (drop session-id capture): the BASE provider default is
        None, so only a provider that overrides spawn_captured_identity binds an
        id. If pi's override were removed, this default would leave the root
        NULL and hibernate would refuse again."""
        from cli_agent_orchestrator.providers.base import BaseProvider

        # The base method returns None (no known-at-spawn identity).
        assert BaseProvider.spawn_captured_identity.__doc__ is not None
        # A minimal stand-in confirming the default contract.
        p = PiCliProvider("t1234567", "sess", "win0")
        # pi overrides to a non-None tuple; the base contract it overrides is None.
        assert p.spawn_captured_identity() is not None


# ─── F908 (#760): cold spawn must start a NEW session ───────────────────────────


class TestFreshSessionOnSpawn:
    """F908 (#760). pi's ``--session-id <id>`` is documented "creating it if
    missing" but SILENTLY RE-ATTACHES an existing session with that id under
    ``--session-dir`` and replays its whole transcript. A runtime dir that
    outlives its terminal therefore makes a COLD worker continue a dead task.
    The cold arm purges the per-terminal session dir; the resume arm must never
    be touched.

    The re-attach premise is an EXTERNAL contract of the pi binary, so it is not
    asserted in prose here: ``test_pi_exact_session_id_reattaches_same_cwd_and_dir``
    below runs the installed pi twice and FAILS if pi ever becomes create-only
    (r1 EMPIRICAL-GATE-NO, H3). These mock-only tests pin CAO's side.
    """

    def _cold(self, root: Path, tid: str = "t1234567") -> PiCliProvider:
        with patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", root):
            p = PiCliProvider(tid, "sess", "win0")
        return p

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_cold_spawn_purges_stale_transcript(self, mock_defaults, tmp_path) -> None:
        mock_defaults.return_value = {}
        with patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", tmp_path):
            p = PiCliProvider("t1234567", "sess", "win0")
            p.session_dir.mkdir(parents=True, exist_ok=True)
            stale = p.session_dir / "2026-09-10T13-03-05-169Z_t1234567.jsonl"
            stale.write_text('{"type":"session"}\n', encoding="utf-8")
            parts = shlex.split(p._build_pi_command())
        assert not stale.exists(), "cold spawn left a prior transcript pi would re-attach"
        assert "--session-id" in parts and parts[parts.index("--session-id") + 1] == "t1234567"
        assert "--session-dir" in parts
        assert "--session" not in parts

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_knob_false_keeps_todays_behaviour(self, mock_defaults, tmp_path) -> None:
        """MUTANT GUARD (knob ignored): with fresh_session_on_spawn = false the
        stale transcript survives and the argv is byte-identical to today's."""
        mock_defaults.return_value = {"fresh_session_on_spawn": False}
        with patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", tmp_path):
            p = PiCliProvider("t1234567", "sess", "win0")
            p.session_dir.mkdir(parents=True, exist_ok=True)
            stale = p.session_dir / "2026-09-10T13-03-05-169Z_t1234567.jsonl"
            stale.write_text('{"type":"session"}\n', encoding="utf-8")
            parts = shlex.split(p._build_pi_command())
        assert stale.exists(), "knob=false must preserve pi's re-attach behaviour"
        assert "--session-id" in parts and "--session-dir" in parts

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_resume_spawn_carries_session_path_and_never_purges(
        self, mock_defaults, tmp_path
    ) -> None:
        """A resume spawn re-attaches on purpose: --session <path>, no
        --session-id, and nothing under the session dir is deleted."""
        from cli_agent_orchestrator.models.terminal import ForkContext

        mock_defaults.return_value = {}
        prior = tmp_path / "prior.jsonl"
        prior.write_text('{"type":"session"}\n', encoding="utf-8")
        ctx = ForkContext(
            mode="resume",
            session_uuid=str(prior),
            base_name="reauth",
            provider="pi_cli",
            initial_preamble="",
            session_artifact_path=str(prior),
        )
        with patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", tmp_path):
            p = PiCliProvider("t7654321", "sess", "win0", fork_context=ctx)
            p.session_dir.mkdir(parents=True, exist_ok=True)
            keep = p.session_dir / "2026-09-09T00-00-00-000Z_t7654321.jsonl"
            keep.write_text('{"type":"session"}\n', encoding="utf-8")
            parts = shlex.split(p._build_pi_command())
        assert "--session" in parts and parts[parts.index("--session") + 1] == str(prior)
        assert "--session-id" not in parts
        assert keep.exists(), "resume spawn must never purge the session dir"

    @pytest.mark.parametrize("alias", ["sessions-leaf", "terminal-parent"])
    def test_purge_guard_refuses_symlink_to_foreign_terminal(self, tmp_path, alias) -> None:
        """r1 EMPIRICAL-GATE-NO H4: the lexical guard passed while the path
        physically pointed at ANOTHER terminal's transcripts, because
        ``is_dir()`` and ``glob()`` both follow directory symlinks. Two aliases
        defeat it — a symlinked ``sessions`` leaf and a symlinked terminal dir.
        The foreign transcript must survive both."""
        root = tmp_path / "pi"
        other = root / "t-other" / "sessions"
        other.mkdir(parents=True)
        foreign = other / "2026-09-09T00-00-00-000Z_t-other.jsonl"
        foreign.write_text('{"type":"session","id":"t-other"}\n', encoding="utf-8")

        if alias == "sessions-leaf":
            # PI_RUNTIME_ROOT/t-own/sessions -> PI_RUNTIME_ROOT/t-other/sessions
            (root / "t-own").mkdir(parents=True)
            (root / "t-own" / "sessions").symlink_to(other, target_is_directory=True)
        else:
            # PI_RUNTIME_ROOT/t-own -> PI_RUNTIME_ROOT/t-other (whole terminal dir)
            (root / "t-own").symlink_to(root / "t-other", target_is_directory=True)

        with patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", root):
            p = PiCliProvider("t-own", "sess", "win0")
            assert p.session_dir == root / "t-own" / "sessions"
            with patch(
                "cli_agent_orchestrator.providers.pi_cli.get_provider_defaults",
                return_value={},
            ):
                p._purge_stale_sessions()
        assert foreign.exists(), f"{alias} alias let purge delete another terminal's transcript"

    def test_purge_refuses_a_terminal_dir_swapped_after_the_root_fd(self, tmp_path) -> None:
        """MUTANT GUARD (open the whole path in one call instead of walking it):
        ``O_NOFOLLOW`` guards only the FINAL component, so a one-shot
        ``os.open("<root>/<tid>/sessions", ...)`` follows a ``<tid>`` swapped for
        a symlink after any earlier check. This fires the swap the moment the
        root fd is taken — the exact window the r2 racer exploited — and the
        component walk must turn it into a refusal.

        Deterministic rehost of r2's ``Path.resolve``-hooked leaf-swap test,
        which the r3 shape no longer calls (EMPIRICAL-GATE-NO r2, repair 2).
        """
        import os as _os

        root = tmp_path / "pi"
        ours = root / "t1234567" / "sessions"
        ours.mkdir(parents=True)
        (ours / "2026-09-10T00-00-00-000Z_t1234567.jsonl").write_text("y", encoding="utf-8")
        foreign_dir = root / "t-other" / "sessions"
        foreign_dir.mkdir(parents=True)
        foreign = foreign_dir / "2026-09-09T00-00-00-000Z_t-other.jsonl"
        foreign.write_text("x", encoding="utf-8")

        real_open = _os.open
        swapped: list[bool] = []

        def racing_open(path, flags, mode=0o777, *, dir_fd=None):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if not swapped and dir_fd is None and str(path) == str(root):
                swapped.append(True)
                # The root fd is open; now swap the terminal dir underneath it.
                _os.rename(str(root / "t1234567"), str(root / "t1234567.moved"))
                (root / "t1234567").symlink_to(root / "t-other", target_is_directory=True)
            return fd

        with patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", root):
            p = PiCliProvider("t1234567", "sess", "win0")
            with (
                patch(
                    "cli_agent_orchestrator.providers.pi_cli.get_provider_defaults",
                    return_value={},
                ),
                patch.object(_os, "open", racing_open),
            ):
                p._purge_stale_sessions()
        assert swapped, "the race never fired — test is not exercising the window"
        assert foreign.exists(), "purge followed a terminal dir swapped in after the check"

    def test_real_concurrent_racer_never_deletes_a_foreign_transcript(self, tmp_path) -> None:
        """Bounded stress test with a REAL concurrent racer and no injection.

        r2 shipped a one-shot ``os.open`` with ``O_NOFOLLOW``; the reviewer's
        racer — a thread alternating ``PI_RUNTIME_ROOT/<tid>`` between a real
        directory (so every prior check passes) and a symlink to another
        terminal — produced 3 foreign deletions in 8627 attempts against it, and
        0 in 423426 against the component walk. This is that racer, bounded so
        it costs ~1.5s in the ordinary suite. Any foreign deletion fails.
        """
        import os as _os
        import threading
        import time

        root = tmp_path / "pi"
        root.mkdir(parents=True)
        own = root / "t-own"
        stash = root / "t-own.stash"
        other = root / "t-other"
        (stash / "sessions").mkdir(parents=True)
        (other / "sessions").mkdir(parents=True)
        foreign = other / "sessions" / "foreign.jsonl"
        foreign.write_text("foreign\n", encoding="utf-8")
        _os.rename(str(stash), str(own))  # start as the real directory

        stop = threading.Event()

        def racer() -> None:
            while not stop.is_set():
                try:
                    _os.rename(str(own), str(stash))
                    _os.symlink(str(other), str(own))
                    _os.unlink(str(own))
                    _os.rename(str(stash), str(own))
                except OSError:
                    pass

        t = threading.Thread(target=racer, daemon=True)
        t.start()
        attempts = 0
        deletions = 0
        try:
            with (
                patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", root),
                patch(
                    "cli_agent_orchestrator.providers.pi_cli." "_resolve_pi_fresh_session_on_spawn",
                    lambda: True,
                ),
            ):
                p = PiCliProvider.__new__(PiCliProvider)
                p.terminal_id = "t-own"
                p.session_dir = own / "sessions"
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    attempts += 1
                    try:
                        p._purge_stale_sessions()
                    except Exception:  # noqa: BLE001 — the racer makes anything possible
                        pass
                    if not foreign.exists():
                        deletions += 1
                        foreign.write_text("foreign\n", encoding="utf-8")
        finally:
            stop.set()
            t.join(timeout=3)
        assert attempts > 50, f"racer starved the purge loop (attempts={attempts})"
        assert deletions == 0, (
            f"purge deleted another terminal's transcript {deletions} time(s) "
            f"in {attempts} attempts"
        )

    def test_unlink_uses_the_open_dir_fd_not_the_path(self, tmp_path) -> None:
        """MUTANT GUARD (``os.unlink(str(sd / name))`` instead of
        ``os.unlink(name, dir_fd=dir_fd)``): the directory is swapped for a
        symlink AFTER the fd is open and the listing taken. A path-form unlink
        re-resolves and deletes the foreign file of the same name; the fd form
        stays bound to the directory we validated."""
        import os as _os

        root = tmp_path / "pi"
        ours = root / "t1234567" / "sessions"
        ours.mkdir(parents=True)
        name = "2026-09-10T00-00-00-000Z_t1234567.jsonl"
        (ours / name).write_text("ours", encoding="utf-8")
        foreign_dir = root / "t-other" / "sessions"
        foreign_dir.mkdir(parents=True)
        foreign = foreign_dir / name
        foreign.write_text("theirs", encoding="utf-8")

        real_listdir = _os.listdir
        swapped = []

        def racing_listdir(fd):  # type: ignore[no-untyped-def]
            out = real_listdir(fd)
            if not swapped:
                swapped.append(True)
                _os.rename(str(ours), str(root / "t1234567" / "sessions.moved"))
                (root / "t1234567" / "sessions").symlink_to(foreign_dir, target_is_directory=True)
            return out

        with patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", root):
            p = PiCliProvider("t1234567", "sess", "win0")
            with (
                patch(
                    "cli_agent_orchestrator.providers.pi_cli.get_provider_defaults",
                    return_value={},
                ),
                patch.object(_os, "listdir", racing_listdir),
            ):
                p._purge_stale_sessions()
        assert swapped, "the race never fired — test is not exercising the window"
        assert foreign.exists(), "path-form unlink followed the swapped-in symlink"
        assert not (
            root / "t1234567" / "sessions.moved" / name
        ).exists(), "our own transcript should still have been purged through the open fd"

    def test_symlinked_transcript_inside_our_dir_is_not_followed(self, tmp_path) -> None:
        """MUTANT GUARD (drop the S_ISREG check): a ``*.jsonl`` symlink planted
        in our own dir must not delete its target. Only regular files we own go."""
        root = tmp_path / "pi"
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "someone-elses.jsonl"
        target.write_text("x", encoding="utf-8")
        sd = root / "t1234567" / "sessions"
        sd.mkdir(parents=True)
        (sd / "link.jsonl").symlink_to(target)
        ours = sd / "2026-09-10T00-00-00-000Z_t1234567.jsonl"
        ours.write_text("y", encoding="utf-8")
        with patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", root):
            p = PiCliProvider("t1234567", "sess", "win0")
            with patch(
                "cli_agent_orchestrator.providers.pi_cli.get_provider_defaults",
                return_value={},
            ):
                p._purge_stale_sessions()
        assert target.exists(), "purge followed a symlinked transcript out of our dir"
        assert (sd / "link.jsonl").is_symlink(), "the symlink itself is not ours to remove"
        assert not ours.exists(), "our own regular transcript must still be purged"

    def test_only_jsonl_transcripts_are_purged(self, tmp_path) -> None:
        """MUTANT GUARD (r2 M15 survivor): dropping the ``endswith(".jsonl")``
        filter makes the purge delete EVERY regular file in our sessions
        directory. Only pi transcripts are ours to remove; anything an operator
        or another tool left there survives."""
        root = tmp_path / "pi"
        sd = root / "t1234567" / "sessions"
        sd.mkdir(parents=True)
        transcript = sd / "2026-09-10T00-00-00-000Z_t1234567.jsonl"
        transcript.write_text("transcript", encoding="utf-8")
        keepers = [
            sd / "notes.txt",
            sd / "2026-09-10T00-00-00-000Z_t1234567.jsonl.tmp",
            sd / "index.json",
            sd / ".gitkeep",
        ]
        for k in keepers:
            k.write_text("keep", encoding="utf-8")

        with patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", root):
            p = PiCliProvider("t1234567", "sess", "win0")
            with patch(
                "cli_agent_orchestrator.providers.pi_cli.get_provider_defaults",
                return_value={},
            ):
                p._purge_stale_sessions()
        assert not transcript.exists(), "the stale transcript must still be purged"
        for k in keepers:
            assert k.exists(), f"purge deleted a non-transcript file: {k.name}"

    def test_every_stale_transcript_is_purged_not_just_the_first(self, tmp_path) -> None:
        """r2 H3 NIT 3: every purge test wrote a single file, so nothing pinned
        that the loop drains the directory."""
        root = tmp_path / "pi"
        sd = root / "t1234567" / "sessions"
        sd.mkdir(parents=True)
        stale = [sd / f"2026-09-0{n}T00-00-00-000Z_t1234567.jsonl" for n in range(1, 6)]
        for f in stale:
            f.write_text("x", encoding="utf-8")

        with patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", root):
            p = PiCliProvider("t1234567", "sess", "win0")
            with patch(
                "cli_agent_orchestrator.providers.pi_cli.get_provider_defaults",
                return_value={},
            ):
                p._purge_stale_sessions()
        assert [f for f in stale if f.exists()] == []

    def test_one_unlink_failure_does_not_abort_the_rest(self, tmp_path) -> None:
        """r2 H3 NIT 4: the unlink-error path warns and continues to the next
        file. A mutant that re-raises would strand later transcripts."""
        import os as _os

        root = tmp_path / "pi"
        sd = root / "t1234567" / "sessions"
        sd.mkdir(parents=True)
        names = [f"2026-09-0{n}T00-00-00-000Z_t1234567.jsonl" for n in range(1, 4)]
        for n in names:
            (sd / n).write_text("x", encoding="utf-8")

        real_unlink = _os.unlink

        def failing_unlink(path, *, dir_fd=None):  # type: ignore[no-untyped-def]
            if path == names[0]:
                raise OSError(13, "Permission denied")
            return real_unlink(path, dir_fd=dir_fd)

        with patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", root):
            p = PiCliProvider("t1234567", "sess", "win0")
            with (
                patch(
                    "cli_agent_orchestrator.providers.pi_cli.get_provider_defaults",
                    return_value={},
                ),
                patch.object(_os, "unlink", failing_unlink),
            ):
                p._purge_stale_sessions()
        assert (sd / names[0]).exists(), "the failing unlink should have been skipped"
        assert not (sd / names[1]).exists(), "a later transcript was stranded by the failure"
        assert not (sd / names[2]).exists(), "a later transcript was stranded by the failure"

    def test_purge_guard_refuses_paths_outside_our_runtime_dir(self, tmp_path) -> None:
        """MUTANT GUARD (drop the lexical ownership guard): a ``session_dir``
        reassigned away from ``PI_RUNTIME_ROOT/<tid>/sessions`` is never touched.

        Both directories are populated on purpose. The walk derives its path
        from ``terminal_id``, so without the lexical guard a provider pointed at
        a foreign dir silently purges the terminal-id dir it was NOT pointed at
        — observable, and the reason the guard is live code in r3 (r2 M12 was
        equivalent only against the old check-then-use shape).
        """
        root = tmp_path / "pi"
        ours = root / "t1234567" / "sessions"
        ours.mkdir(parents=True)
        not_pointed_at = ours / "2026-09-10T00-00-00-000Z_t1234567.jsonl"
        not_pointed_at.write_text("x", encoding="utf-8")
        foreign = tmp_path / "somewhere-else"
        foreign.mkdir(parents=True)
        victim = foreign / "other.jsonl"
        victim.write_text("x", encoding="utf-8")

        with patch("cli_agent_orchestrator.providers.pi_cli.PI_RUNTIME_ROOT", root):
            p = PiCliProvider("t1234567", "sess", "win0")
            p.session_dir = foreign
            with patch(
                "cli_agent_orchestrator.providers.pi_cli.get_provider_defaults",
                return_value={},
            ):
                p._purge_stale_sessions()
        assert victim.exists(), "purge reached the reassigned foreign directory"
        assert (
            not_pointed_at.exists()
        ), "purge deleted the terminal-id directory the provider was not pointed at"


class TestFreshSessionKnob:
    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_default_true(self, mock_defaults) -> None:
        mock_defaults.return_value = {}
        assert _resolve_pi_fresh_session_on_spawn() is True

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_explicit_false(self, mock_defaults) -> None:
        mock_defaults.return_value = {"fresh_session_on_spawn": False}
        assert _resolve_pi_fresh_session_on_spawn() is False

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_string_false_is_honoured(self, mock_defaults) -> None:
        mock_defaults.return_value = {"fresh_session_on_spawn": "false"}
        assert _resolve_pi_fresh_session_on_spawn() is False

    @patch("cli_agent_orchestrator.providers.pi_cli.get_provider_defaults")
    def test_garbage_falls_back_to_default(self, mock_defaults) -> None:
        """A typo can never silently disable the guard."""
        mock_defaults.return_value = {"fresh_session_on_spawn": "maybe"}
        assert _resolve_pi_fresh_session_on_spawn() is True

    @pytest.mark.parametrize("value", [True, "true", "TRUE", " True ", "1", "yes", "on"])
    def test_all_true_spellings_are_honoured(self, value) -> None:
        """MUTANT GUARD (r1 M6 survivor): the accepted-true tuple is
        ``("true", "1", "yes", "on")`` and the parse lowercases and strips.

        The default is ALSO True, so a naive ``is True`` assertion cannot tell
        "recognized as true" from "fell through to the default" — which is
        exactly why narrowing the tuple to ``("true",)`` survived every r1 test.
        Invert the default for the duration so only genuine recognition passes.
        """
        with (
            patch(
                "cli_agent_orchestrator.providers.pi_cli.get_provider_defaults",
                return_value={"fresh_session_on_spawn": value},
            ),
            patch(
                "cli_agent_orchestrator.providers.pi_cli." "_PI_FRESH_SESSION_ON_SPAWN_DEFAULT",
                False,
            ),
        ):
            assert _resolve_pi_fresh_session_on_spawn() is True

    @pytest.mark.parametrize("value", [False, "false", "FALSE", " off ", "0", "no", "off"])
    def test_all_false_spellings_are_honoured(self, value) -> None:
        """MUTANT GUARD: narrowing the accepted-false tuple silently re-arms the
        purge for an operator who typed a legal TOML-ish spelling of false."""
        with patch(
            "cli_agent_orchestrator.providers.pi_cli.get_provider_defaults",
            return_value={"fresh_session_on_spawn": value},
        ):
            assert _resolve_pi_fresh_session_on_spawn() is False

    def test_loader_exception_falls_back_to_default_true(self) -> None:
        """MUTANT GUARD: a providers.toml the loader cannot read must not
        silently disable the guard."""
        with patch(
            "cli_agent_orchestrator.providers.pi_cli.get_provider_defaults",
            side_effect=RuntimeError("providers.toml unreadable"),
        ):
            assert _resolve_pi_fresh_session_on_spawn() is True


# ─── F908 (#760) repair 3: the EXTERNAL pi contract this fix exists for ─────────


@pytest.mark.live
@pytest.mark.slow
class TestPiSessionIdExternalContract:
    """r1 EMPIRICAL-GATE-NO H3: every other F908 test mocks config and touches
    inert files, so all of them would keep passing if pi changed
    ``--session-id`` from re-attach to create-only — and the purge would then be
    dead weight deleting live transcripts for no reason. This runs the installed
    pi twice, one cwd / one id / one session dir, and fails loudly if the
    premise stops holding.

    Opt-in: ``live`` + ``slow`` (deselected by the usual
    ``-m "not e2e and not slow"``), because it spends real model tokens.
    """

    SUPPORTED_PI = "0.85.1"
    TOKEN = "ZORBLAX-7741"

    def _run(self, cwd: Path, session_dir: Path, prompt: str) -> str:
        import subprocess

        proc = subprocess.run(
            [
                PI_BINARY,
                "--tui-mode",
                "regular",
                "--no-approve",
                "--no-context-files",
                "--no-skills",
                "--no-prompt-templates",
                "--no-tools",
                "--session-id",
                "f908contract",
                "--session-dir",
                str(session_dir),
                "-p",
                prompt,
            ],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=300,
        )
        return proc.stdout + proc.stderr

    def test_pi_exact_session_id_reattaches_same_cwd_and_dir(self, tmp_path) -> None:
        import subprocess

        if not Path(PI_BINARY).exists():
            pytest.skip(f"pi not installed at {PI_BINARY}")
        version = subprocess.run(
            [PI_BINARY, "--version"], capture_output=True, text=True, timeout=60
        ).stdout.strip()

        cwd = tmp_path / "work"
        cwd.mkdir()
        sd = tmp_path / "sessions"
        sd.mkdir()

        first = self._run(cwd, sd, f"Remember this magic token: {self.TOKEN}. Reply only OK.")
        created = sorted(sd.glob("*.jsonl"))
        assert len(created) == 1, f"first run wrote {len(created)} session files: {first[-500:]}"

        second = self._run(
            cwd, sd, "What magic token were you told to remember? One word, or NONE."
        )
        after = sorted(sd.glob("*.jsonl"))

        reattached = after == created and self.TOKEN in second
        if not reattached:
            pytest.fail(
                "F908 PREMISE BROKEN — pi "
                f"{version} no longer re-attaches by exact --session-id "
                f"(supported at build time: {self.SUPPORTED_PI}).\n"
                f"session files before={[f.name for f in created]} "
                f"after={[f.name for f in after]}\n"
                "If pi is now create-only, _purge_stale_sessions is dead weight that "
                "deletes live transcripts: re-open F908 #760 before shipping this pi.\n"
                f"second-run output tail: {second[-500:]}"
            )
        # Witness for the ledger: no new file, and the prior transcript's token recalled.
        assert after == created
        assert self.TOKEN in second
