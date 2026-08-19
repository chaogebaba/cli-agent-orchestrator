"""Unit tests for Cline CLI provider (plain-mode one-shot invocations).

Tests cover:
  - Command construction (base args, model/provider/thinking/system-prompt resolution)
  - Dispatcher script generation (escaping torture cases)
  - Status detection via pane_current_command baseline
  - Session-id correlation via history snapshot
  - Message file handling
  - Response extraction from scrollback
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.cline_cli import (
    CLINE_BINARY,
    DISPATCHER_IDLE_CMD,
    ERROR_PATTERN,
    SCRATCH_DIR,
    ClineCliProvider,
    _build_dispatcher_script,
)


# ─── Dispatcher script generation ────────────────────────────────────────────


class TestDispatcherScript:
    """Tests for _build_dispatcher_script."""

    def test_basic_script_structure(self):
        """Script has the expected loop structure."""
        script = _build_dispatcher_script(
            "/home/user/.bun/bin/cline",
            "/data/cao-scratch/cline-msgs",
            "t1234567",
            "/home/user/.bun/bin/cline --auto-approve true -P cline-pass",
        )
        assert "while true" in script
        assert "cat >" in script
        assert "_cao_msg_n=" in script
        assert "t1234567" in script
        assert "/data/cao-scratch/cline-msgs" in script
        assert '--auto-approve true' in script

    def test_script_uses_cat_for_idle(self):
        """Script uses cat as the blocking read (idle sentinel)."""
        script = _build_dispatcher_script(
            CLINE_BINARY, "/tmp/msgs", "tABCDEFG", f"{CLINE_BINARY} --auto-approve true"
        )
        assert "cat >" in script

    def test_script_invokes_cline_with_cat_substitution(self):
        """Script invokes cline with $(cat <file>) for safe escaping."""
        script = _build_dispatcher_script(
            CLINE_BINARY, "/data/msgs", "t1234567",
            f"{CLINE_BINARY} --auto-approve true -P cline-pass --thinking high"
        )
        assert '"$(cat "$_cao_msgfile")"' in script

    def test_script_skips_empty_files(self):
        """Script skips empty message files ([ -s ] check)."""
        script = _build_dispatcher_script(
            CLINE_BINARY, "/data/msgs", "t1234567", f"{CLINE_BINARY} --auto-approve true"
        )
        assert '[ -s "$_cao_msgfile" ] || continue' in script

    def test_script_increments_message_counter(self):
        """Script increments _cao_msg_n for unique filenames."""
        script = _build_dispatcher_script(
            CLINE_BINARY, "/data/msgs", "t1234567", f"{CLINE_BINARY} --auto-approve true"
        )
        assert "_cao_msg_n=$((_cao_msg_n + 1))" in script


# ─── Command construction (base args) ────────────────────────────────────────


class TestClineCliBaseArgs:
    """Tests for _build_base_args (command construction)."""

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_default_base_args(self, mock_load, mock_defaults):
        """Default: --auto-approve true, -P cline-pass, --thinking high."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"api_provider": "cline-pass"}

        provider = ClineCliProvider("t1234567", "sess", "win0")
        args = provider._build_base_args()
        parts = shlex.split(args)

        assert parts[0] == CLINE_BINARY
        assert "--auto-approve" in parts
        assert "true" in parts
        assert "-P" in parts
        assert parts[parts.index("-P") + 1] == "cline-pass"
        assert "--thinking" in parts
        assert parts[parts.index("--thinking") + 1] == "high"
        # No --tui (plain mode)
        assert "--tui" not in parts
        assert "-i" not in parts
        # No -m without model override
        assert "-m" not in parts
        assert "-s" not in parts

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_base_args_with_model_override(self, mock_load, mock_defaults):
        """Explicit model kwarg is forwarded as -m."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"api_provider": "cline-pass"}

        provider = ClineCliProvider(
            "t1234567", "sess", "win0", model="deepseek/deepseek-chat"
        )
        args = provider._build_base_args()
        parts = shlex.split(args)

        assert "-m" in parts
        assert parts[parts.index("-m") + 1] == "deepseek/deepseek-chat"

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_base_args_with_system_prompt(self, mock_load, mock_defaults):
        """Profile system prompt is forwarded as -s."""
        mock_defaults.return_value = {"api_provider": "cline-pass"}
        profile = MagicMock()
        profile.system_prompt = "You are a test agent."
        profile.model = None
        profile.name = "test-agent"
        mock_load.return_value = profile

        provider = ClineCliProvider("t1234567", "sess", "win0", agent_profile="test-agent")
        args = provider._build_base_args()
        parts = shlex.split(args)

        assert "-s" in parts
        assert parts[parts.index("-s") + 1] == "You are a test agent."

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_base_args_skill_prompt_appended(self, mock_load, mock_defaults):
        """Skill prompt is appended to system prompt."""
        mock_defaults.return_value = {"api_provider": "cline-pass"}
        profile = MagicMock()
        profile.system_prompt = "Base prompt."
        profile.model = None
        profile.name = "agent"
        mock_load.return_value = profile

        provider = ClineCliProvider(
            "t1234567", "sess", "win0",
            agent_profile="agent",
            skill_prompt="Extra skill instructions.",
        )
        args = provider._build_base_args()
        parts = shlex.split(args)

        assert "-s" in parts
        system_arg = parts[parts.index("-s") + 1]
        assert "Base prompt." in system_arg
        assert "Extra skill instructions." in system_arg

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_no_tui_flag_in_base_args(self, mock_load, mock_defaults):
        """Plain mode: no --tui or -i flag."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"api_provider": "cline-pass"}

        provider = ClineCliProvider("t1234567", "sess", "win0")
        args = provider._build_base_args()

        assert "--tui" not in args
        assert " -i " not in args


# ─── Model resolution ────────────────────────────────────────────────────────


class TestClineCliModelResolution:
    """Tests for model resolution precedence."""

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_profile_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.resolve_provider_string_option")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_explicit_model_wins(self, mock_load, mock_resolve, mock_prof_defaults, mock_defaults):
        """Explicit model kwarg takes precedence over everything."""
        mock_load.side_effect = FileNotFoundError("no profile")

        provider = ClineCliProvider(
            "t1234567", "sess", "win0", model="deepseek/deepseek-r1"
        )
        result = provider._resolve_model()

        assert result == "deepseek/deepseek-r1"
        mock_resolve.assert_not_called()

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_profile_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.resolve_provider_string_option")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_toml_resolution_fallback(
        self, mock_load, mock_resolve, mock_prof_defaults, mock_defaults
    ):
        """Without explicit model, falls back to providers.toml chain."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"model": "deepseek/deepseek-chat"}
        mock_prof_defaults.return_value = {}
        mock_resolve.return_value = "deepseek/deepseek-chat"

        provider = ClineCliProvider("t1234567", "sess", "win0")
        result = provider._resolve_model()

        assert result == "deepseek/deepseek-chat"
        mock_defaults.assert_called_once_with("cline_cli")

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_resolved_model_property(self, mock_load, mock_defaults):
        """resolved_model is set after _build_base_args."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"api_provider": "cline-pass"}

        provider = ClineCliProvider(
            "t1234567", "sess", "win0", model="deepseek/deepseek-v4-flash"
        )
        provider._build_base_args()

        assert provider.resolved_model == "deepseek/deepseek-v4-flash"


# ─── Thinking / reasoning effort ──────────────────────────────────────────────


class TestClineCliThinking:
    """Tests for --thinking flag resolution."""

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_profile_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.resolve_provider_string_option")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_default_thinking_high(self, mock_load, mock_resolve, mock_prof, mock_defaults):
        """Without any override, thinking defaults to 'high'."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {}
        mock_prof.return_value = {}
        mock_resolve.return_value = None

        provider = ClineCliProvider("t1234567", "sess", "win0")
        result = provider._resolve_thinking()
        assert result == "high"

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_profile_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.resolve_provider_string_option")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_toml_thinking_override(self, mock_load, mock_resolve, mock_prof, mock_defaults):
        """providers.toml thinking value overrides the default."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"thinking": "medium"}
        mock_prof.return_value = {}
        mock_resolve.return_value = "medium"

        provider = ClineCliProvider("t1234567", "sess", "win0")
        result = provider._resolve_thinking()
        assert result == "medium"

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_thinking_in_base_args(self, mock_load, mock_defaults):
        """--thinking flag appears in the built base args."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"api_provider": "cline-pass", "thinking": "xhigh"}

        provider = ClineCliProvider("t1234567", "sess", "win0")
        parts = shlex.split(provider._build_base_args())

        assert "--thinking" in parts
        assert parts[parts.index("--thinking") + 1] == "xhigh"

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_profile_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.resolve_provider_string_option")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_explicit_empty_suppresses_thinking(self, mock_load, mock_resolve, mock_prof, mock_defaults):
        """Explicit empty string in providers.toml suppresses the --thinking flag."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"thinking": ""}
        mock_prof.return_value = {}
        mock_resolve.return_value = ""

        provider = ClineCliProvider("t1234567", "sess", "win0")
        assert provider._resolve_thinking() == ""

    @patch("cli_agent_orchestrator.providers.cline_cli.resolve_provider_string_option")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_profile_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_empty_thinking_omits_flag_from_base_args(self, mock_load, mock_defaults, mock_prof, mock_resolve):
        """Empty thinking value results in no --thinking flag in base args."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"api_provider": "cline-pass"}
        mock_prof.return_value = {}
        # First call for model (returns None), second call for thinking (returns "")
        mock_resolve.side_effect = [None, ""]

        provider = ClineCliProvider("t1234567", "sess", "win0")
        parts = shlex.split(provider._build_base_args())

        assert "--thinking" not in parts


# ─── Status detection ─────────────────────────────────────────────────────────


class TestClineCliStatusDetection:
    """Tests for get_status based on pane_current_command."""

    def _make_provider(self, initialized=True, dispatched=False) -> ClineCliProvider:
        provider = ClineCliProvider("t1234567", "sess", "win0")
        provider._initialized = initialized
        provider._task_dispatched_flag = dispatched
        provider.shell_baseline = "zsh"
        return provider

    @patch("cli_agent_orchestrator.providers.cline_cli.get_backend")
    def test_idle_before_dispatch(self, mock_backend):
        """pane_current_command == 'cat' before any dispatch → IDLE."""
        mock_backend.return_value.get_pane_current_command.return_value = "cat"
        provider = self._make_provider(initialized=True, dispatched=False)
        assert provider.get_status("") == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.cline_cli.get_backend")
    def test_completed_after_dispatch(self, mock_backend):
        """pane_current_command == 'cat' after dispatch → COMPLETED."""
        mock_backend.return_value.get_pane_current_command.return_value = "cat"
        provider = self._make_provider(initialized=True, dispatched=True)
        assert provider.get_status("") == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.cline_cli.get_backend")
    def test_processing_cline_running(self, mock_backend):
        """pane_current_command == 'cline' → PROCESSING."""
        mock_backend.return_value.get_pane_current_command.return_value = "cline"
        provider = self._make_provider(initialized=True, dispatched=True)
        assert provider.get_status("") == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.cline_cli.get_backend")
    def test_processing_node_running(self, mock_backend):
        """pane_current_command == 'node' (cline subprocess) → PROCESSING."""
        mock_backend.return_value.get_pane_current_command.return_value = "node"
        provider = self._make_provider(initialized=True, dispatched=True)
        assert provider.get_status("") == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.cline_cli.get_backend")
    def test_error_shell_baseline(self, mock_backend):
        """pane_current_command == shell_baseline → ERROR (dispatcher crashed)."""
        mock_backend.return_value.get_pane_current_command.return_value = "zsh"
        provider = self._make_provider(initialized=True, dispatched=True)
        assert provider.get_status("") == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.cline_cli.get_backend")
    def test_error_shell_baseline_before_dispatch(self, mock_backend):
        """Shell baseline before dispatch also = ERROR."""
        mock_backend.return_value.get_pane_current_command.return_value = "zsh"
        provider = self._make_provider(initialized=True, dispatched=False)
        assert provider.get_status("") == TerminalStatus.ERROR

    def test_uninitialized_unknown(self):
        """Uninitialized provider → UNKNOWN."""
        provider = self._make_provider(initialized=False)
        assert provider.get_status("") == TerminalStatus.UNKNOWN

    @patch("cli_agent_orchestrator.providers.cline_cli.get_backend")
    def test_session_id_correlation_on_completion(self, mock_backend):
        """Session ID is correlated on first COMPLETED detection."""
        mock_backend.return_value.get_pane_current_command.return_value = "cat"
        provider = self._make_provider(initialized=True, dispatched=True)
        provider._message_count = 1
        provider._pre_run_history_ids = {"old_session_1"}

        with patch.object(provider, "_snapshot_history_ids") as mock_snapshot:
            mock_snapshot.return_value = {"old_session_1", "new_session_abc"}
            status = provider.get_status("")

        assert status == TerminalStatus.COMPLETED
        assert provider._session_id == "new_session_abc"

    @patch("cli_agent_orchestrator.providers.cline_cli.get_backend")
    def test_session_id_not_re_correlated(self, mock_backend):
        """Once session_id is set, don't re-correlate."""
        mock_backend.return_value.get_pane_current_command.return_value = "cat"
        provider = self._make_provider(initialized=True, dispatched=True)
        provider._message_count = 1
        provider._session_id = "already_set"

        with patch.object(provider, "_correlate_session_id") as mock_correlate:
            provider.get_status("")
            mock_correlate.assert_not_called()


# ─── Session-id correlation ───────────────────────────────────────────────────


class TestClineCliSessionCorrelation:
    """Tests for history snapshot and session correlation."""

    def _make_provider(self) -> ClineCliProvider:
        return ClineCliProvider("t1234567", "sess", "win0")

    @patch("cli_agent_orchestrator.providers.cline_cli.subprocess.run")
    def test_snapshot_history_ids_success(self, mock_run):
        """Successful history snapshot returns session IDs."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {"sessionId": "sess_001", "status": "completed"},
                {"sessionId": "sess_002", "status": "completed"},
            ]),
        )
        provider = self._make_provider()
        ids = provider._snapshot_history_ids()
        assert ids == {"sess_001", "sess_002"}

    @patch("cli_agent_orchestrator.providers.cline_cli.subprocess.run")
    def test_snapshot_history_ids_failure(self, mock_run):
        """Failed history command returns empty set."""
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        provider = self._make_provider()
        ids = provider._snapshot_history_ids()
        assert ids == set()

    @patch("cli_agent_orchestrator.providers.cline_cli.subprocess.run")
    def test_snapshot_history_ids_timeout(self, mock_run):
        """Timeout returns empty set."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired("cline", 10)
        provider = self._make_provider()
        ids = provider._snapshot_history_ids()
        assert ids == set()

    def test_correlate_single_new_id(self):
        """Single new ID after invocation is correctly correlated."""
        provider = self._make_provider()
        provider._pre_run_history_ids = {"sess_001", "sess_002"}
        with patch.object(provider, "_snapshot_history_ids") as mock_snap:
            mock_snap.return_value = {"sess_001", "sess_002", "sess_003"}
            result = provider._correlate_session_id()
        assert result == "sess_003"

    def test_correlate_ambiguous_multiple_new(self):
        """Multiple new IDs → ambiguous, returns None."""
        provider = self._make_provider()
        provider._pre_run_history_ids = {"sess_001"}
        with patch.object(provider, "_snapshot_history_ids") as mock_snap:
            mock_snap.return_value = {"sess_001", "sess_002", "sess_003"}
            result = provider._correlate_session_id()
        assert result is None

    def test_correlate_no_new_ids(self):
        """No new IDs → returns None."""
        provider = self._make_provider()
        provider._pre_run_history_ids = {"sess_001", "sess_002"}
        with patch.object(provider, "_snapshot_history_ids") as mock_snap:
            mock_snap.return_value = {"sess_001", "sess_002"}
            result = provider._correlate_session_id()
        assert result is None


# ─── Escaping torture cases ───────────────────────────────────────────────────


class TestClineCliEscaping:
    """Tests that the dispatcher script handles difficult message content.

    The dispatcher writes message content to a file via cat, then uses
    $(cat <file>) to pass it to cline. This should survive all special chars.
    """

    def test_script_handles_quotes_in_base_args(self):
        """System prompt with quotes is properly escaped in base_args."""
        provider = ClineCliProvider("t1234567", "sess", "win0")
        with patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults") as mock_defaults:
            mock_defaults.return_value = {"api_provider": "cline-pass"}
            with patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile") as mock_load:
                profile = MagicMock()
                profile.system_prompt = 'Say "hello" and \'goodbye\''
                profile.model = None
                profile.name = "quotey"
                mock_load.return_value = profile
                provider._agent_profile = "quotey"

                args = provider._build_base_args()
                # Verify the args string is valid shell (can be parsed back)
                parts = shlex.split(args)
                assert "-s" in parts
                system_arg = parts[parts.index("-s") + 1]
                assert '"hello"' in system_arg
                assert "'goodbye'" in system_arg

    def test_message_file_content_survives_special_chars(self):
        """The file-based approach survives newlines, quotes, backticks, $."""
        # This tests the concept: cat > file writes EXACTLY what stdin receives.
        # The $(cat file) expansion then passes that content as a single argument.
        # No escaping needed in the message itself — only the file path needs quoting.
        difficult_message = (
            'Hello "world"\n'
            "It's a test\n"
            "Price is $100\n"
            "Run `echo foo`\n"
            "Backslash: \\\n"
            "Semicolon; ampersand& pipe|\n"
        )
        # The dispatcher script template uses fixed paths — message goes to cat's stdin.
        # Verify the script doesn't try to inline-escape the message.
        script = _build_dispatcher_script(
            CLINE_BINARY, "/data/msgs", "tESCAPE1",
            f"{CLINE_BINARY} --auto-approve true"
        )
        # The script should NOT contain any message content — it reads from stdin.
        assert difficult_message not in script
        # The $(cat "$_cao_msgfile") construct handles the escaping.
        assert '"$(cat "$_cao_msgfile")"' in script


# ─── Response extraction ──────────────────────────────────────────────────────


class TestClineCliExtraction:
    """Tests for extract_last_message_from_script."""

    def _make_provider(self) -> ClineCliProvider:
        return ClineCliProvider("t1234567", "sess", "win0")

    def test_basic_extraction(self):
        """Extract response between cline invocation and next dispatcher prompt."""
        provider = self._make_provider()
        script = (
            f"{CLINE_BINARY} --auto-approve true \"$(cat /data/msgs/t1234567_1.txt)\"\n"
            "The answer is 42.\n"
            "\n"
        )
        result = provider.extract_last_message_from_script(script)
        assert result == "The answer is 42."

    def test_multiline_extraction(self):
        """Multi-line response is extracted correctly."""
        provider = self._make_provider()
        script = (
            f"{CLINE_BINARY} --auto-approve true \"$(cat /data/msgs/t1234567_1.txt)\"\n"
            "Line one.\n"
            "Line two.\n"
            "Line three.\n"
        )
        result = provider.extract_last_message_from_script(script)
        assert "Line one." in result
        assert "Line three." in result

    def test_stops_at_dispatcher_prompt(self):
        """Extraction stops at next dispatcher cat > line."""
        provider = self._make_provider()
        script = (
            f"{CLINE_BINARY} --auto-approve true \"$(cat /data/msgs/t1234567_1.txt)\"\n"
            "Response text here.\n"
            "cat > /data/msgs/t1234567_2.txt\n"
            "This should not be included.\n"
        )
        result = provider.extract_last_message_from_script(script)
        assert result == "Response text here."
        assert "not be included" not in result

    def test_no_invocation_fallback(self):
        """Without a cline invocation line, falls back to all non-empty content."""
        provider = self._make_provider()
        script = "Just some random output.\nAnother line.\n"
        result = provider.extract_last_message_from_script(script)
        assert "Just some random output." in result
        assert "Another line." in result

    def test_empty_response_raises(self):
        """Empty response after cline invocation raises ValueError."""
        provider = self._make_provider()
        script = f"{CLINE_BINARY} --auto-approve true \"$(cat /data/msgs/t1234567_1.txt)\"\n\n"
        with pytest.raises(ValueError, match="Empty Cline response"):
            provider.extract_last_message_from_script(script)

    def test_no_content_at_all_raises(self):
        """Completely empty input with no cline line and no content raises."""
        provider = self._make_provider()
        with pytest.raises(ValueError, match="No cline invocation"):
            provider.extract_last_message_from_script("")


# ─── Provider registration ────────────────────────────────────────────────────


class TestClineCliRegistration:
    """Tests that the provider is correctly registered."""

    def test_provider_type_enum_exists(self):
        assert ProviderType.CLINE_CLI.value == "cline_cli"

    def test_provider_in_classes_map(self):
        from cli_agent_orchestrator.providers.manager import PROVIDER_CLASSES

        assert "cline_cli" in PROVIDER_CLASSES
        assert PROVIDER_CLASSES["cline_cli"] is ClineCliProvider

    def test_provider_manager_creates_cline(self):
        from cli_agent_orchestrator.providers.manager import ProviderManager

        mgr = ProviderManager()
        provider = mgr.construct_provider(
            "cline_cli",
            "t1234567",
            "test-session",
            "win-0",
            agent_profile="developer",
            model="deepseek/deepseek-v4-flash",
        )
        assert isinstance(provider, ClineCliProvider)
        assert provider._model == "deepseek/deepseek-v4-flash"

    def test_cline_in_soft_enforcement_providers(self):
        """S2: cline_cli is in SOFT_ENFORCEMENT_PROVIDERS (no native tool blocking)."""
        from cli_agent_orchestrator.services.terminal_service import (
            SOFT_ENFORCEMENT_PROVIDERS,
        )

        assert "cline_cli" in SOFT_ENFORCEMENT_PROVIDERS


# ─── Miscellaneous properties ─────────────────────────────────────────────────


class TestClineCliProperties:
    """Tests for provider properties and interface compliance."""

    def test_paste_enter_count(self):
        provider = ClineCliProvider("t1234567", "sess", "win0")
        assert provider.paste_enter_count == 1

    def test_paste_submit_delay(self):
        provider = ClineCliProvider("t1234567", "sess", "win0")
        assert provider.paste_submit_delay == 0.1

    def test_exit_cli(self):
        provider = ClineCliProvider("t1234567", "sess", "win0")
        assert provider.exit_cli() == "exit"

    def test_cleanup(self):
        provider = ClineCliProvider("t1234567", "sess", "win0")
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False

    def test_classify_injection_hazard_none(self):
        """Plain one-shot mode has no injection hazard."""
        provider = ClineCliProvider("t1234567", "sess", "win0")
        rows = ["some text", "more text"]
        assert provider.classify_injection_hazard(rows) is None

    def test_session_id_property(self):
        """session_id property returns the correlated ID."""
        provider = ClineCliProvider("t1234567", "sess", "win0")
        assert provider.session_id is None
        provider._session_id = "test_session_123"
        assert provider.session_id == "test_session_123"

    def test_dispatcher_idle_cmd_is_cat(self):
        """The idle sentinel command is 'cat'."""
        assert DISPATCHER_IDLE_CMD == "cat"


# ─── EOF dispatch (Ctrl-D) ────────────────────────────────────────────────────


class TestClineCliEofDispatch:
    """Tests for the Ctrl-D EOF dispatch in _after_dispatch_commit_locked."""

    @patch("cli_agent_orchestrator.providers.cline_cli.get_backend")
    @patch("cli_agent_orchestrator.providers.cline_cli.subprocess.run")
    def test_after_dispatch_sends_eof(self, mock_subprocess, mock_backend):
        """_after_dispatch_commit_locked spawns a thread that sends C-d."""
        import time

        # Mock subprocess for history snapshot
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="[]"
        )

        provider = ClineCliProvider("t1234567", "sess", "win0")
        provider._after_dispatch_commit_locked()

        # Wait for the thread to execute
        time.sleep(0.5)

        mock_backend.return_value.send_special_key.assert_called_once_with(
            "sess", "win0", "C-d"
        )

    @patch("cli_agent_orchestrator.providers.cline_cli.get_backend")
    @patch("cli_agent_orchestrator.providers.cline_cli.subprocess.run")
    def test_after_dispatch_sets_flags(self, mock_subprocess, mock_backend):
        """_after_dispatch_commit_locked sets task_dispatched and increments count."""
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="[]")

        provider = ClineCliProvider("t1234567", "sess", "win0")
        assert provider._task_dispatched_flag is False
        assert provider._message_count == 0

        provider._after_dispatch_commit_locked()

        assert provider._task_dispatched_flag is True
        assert provider._message_count == 1

    @patch("cli_agent_orchestrator.providers.cline_cli.get_backend")
    @patch("cli_agent_orchestrator.providers.cline_cli.subprocess.run")
    def test_after_dispatch_snapshots_history(self, mock_subprocess, mock_backend):
        """_after_dispatch_commit_locked snapshots history for correlation."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{"sessionId": "existing_1"}]),
        )

        provider = ClineCliProvider("t1234567", "sess", "win0")
        provider._after_dispatch_commit_locked()

        assert provider._pre_run_history_ids == {"existing_1"}
