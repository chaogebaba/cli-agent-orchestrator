"""FX-F115 reap-race lifecycle tests: exit-suppress, early clear, last-line recheck."""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderCapabilities
from cli_agent_orchestrator.services import auto_responder as ar

FIXTURES = Path(__file__).parents[1] / "fixtures" / "f115"


def _fixture(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def _metadata(**overrides):
    metadata = {
        "id": "term1",
        "tmux_session": "cao-sess",
        "tmux_window": "win",
        "provider": "grok_cli",
        "provider_session_id": None,
        "lifecycle_generation": 7,
    }
    metadata.update(overrides)
    return metadata


def _wire(monkeypatch, metadata, backend, *, supervisors=None):
    if supervisors is None:
        supervisors = [{"id": "sup1", "provider": "claude_code"}]
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda terminal_id: metadata if terminal_id == metadata["id"] else None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_env.get_session_env", lambda _session: {}
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.list_terminals_by_session",
        lambda _session: list(supervisors),
    )
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.seam_activation.receiver_state_active",
        lambda _operation: False,
    )


def _backend(screen: list[str] | None = None):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.capture_viewport.return_value = "\n".join(screen or [])
    return backend


class GrokLikeProvider:
    """Minimal provider that classifies WAITING_USER_ANSWER for grok-like footer."""

    capabilities = ProviderCapabilities(supports_screen_detection=True)

    def __init__(self):
        self._exit_command = "/exit"

    def get_status_from_screen(self, lines):
        text = " ".join(lines)
        if "Enter:submit" in text or "↑/↓ navigate" in text:
            return TerminalStatus.WAITING_USER_ANSWER
        return TerminalStatus.UNKNOWN

    def exit_cli(self):
        return self._exit_command


# ---------------------------------------------------------------------------
# T1: F115 fixture no-push when suppressed
# ---------------------------------------------------------------------------


class TestT1FixtureNoPushWhenSuppressed:
    """After exit_terminal_cli, on_screen on the F115 fixture must not push."""

    def test_suppressed_terminal_returns_none(self, monkeypatch, tmp_path):
        """Suppressed terminal: on_screen returns None, no push created."""
        screen = _fixture("exited-terminal-residue.txt")
        provider = GrokLikeProvider()
        metadata = _metadata()
        backend = _backend(screen)
        _wire(monkeypatch, metadata, backend)
        monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path / "rules")
        monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path / "logs")
        pushed = []
        monkeypatch.setattr(ar.AutoResponder, "_push", lambda self, *args: pushed.append(args))

        engine = ar.AutoResponder()
        # Simulate exit_terminal_cli marking suppress
        engine.mark_exit_suppress("term1")

        result = engine.on_screen("term1", provider, screen)

        assert result is None
        assert pushed == []

    def test_exit_terminal_cli_marks_suppress(self, monkeypatch):
        """The real exit_terminal_cli body marks exit-suppress via send_input mock."""
        from cli_agent_orchestrator.services.auto_responder import auto_responder
        from cli_agent_orchestrator.services import terminal_service

        provider = GrokLikeProvider()
        # Mock provider_manager.get_provider to return our provider
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.provider_manager.get_provider",
            lambda tid: provider,
        )
        # Mock send_input (the /exit text dispatch path)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.send_input",
            lambda *args, **kwargs: None,
        )
        # Clear any prior state
        auto_responder.unmark_exit_suppress("term1")
        assert not auto_responder.is_exit_suppressed("term1")

        terminal_service.exit_terminal_cli("term1")

        assert auto_responder.is_exit_suppressed("term1")
        # Cleanup
        auto_responder.unmark_exit_suppress("term1")

    def test_exit_terminal_cli_special_key_marks_suppress(self, monkeypatch):
        """exit_terminal_cli with C- prefix also marks suppress."""
        from cli_agent_orchestrator.services.auto_responder import auto_responder
        from cli_agent_orchestrator.services import terminal_service

        class CtrlDProvider:
            capabilities = ProviderCapabilities(supports_screen_detection=True)

            def exit_cli(self):
                return "C-d"

        provider = CtrlDProvider()
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.provider_manager.get_provider",
            lambda tid: provider,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.send_special_key",
            lambda *args, **kwargs: None,
        )
        auto_responder.unmark_exit_suppress("term1")

        terminal_service.exit_terminal_cli("term1")

        assert auto_responder.is_exit_suppressed("term1")
        auto_responder.unmark_exit_suppress("term1")


# ---------------------------------------------------------------------------
# T2: F115 fixture no-push when metadata gone
# ---------------------------------------------------------------------------


class TestT2NoPushWhenMetadataGone:
    """metadata=None mid-path: no push (Layer A + existing _on_screen guard)."""

    def test_no_metadata_returns_none(self, monkeypatch, tmp_path):
        screen = _fixture("exited-terminal-residue.txt")
        provider = GrokLikeProvider()
        backend = _backend(screen)
        # Simulate metadata gone
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            lambda terminal_id: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_env.get_session_env", lambda _session: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            lambda _session: [{"id": "sup1", "provider": "claude_code"}],
        )
        monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.seam_activation.receiver_state_active",
            lambda _operation: False,
        )
        monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path / "rules")
        monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path / "logs")
        pushed = []
        monkeypatch.setattr(ar.AutoResponder, "_push", lambda self, *args: pushed.append(args))

        engine = ar.AutoResponder()
        result = engine.on_screen("term1", provider, screen)

        assert result is None
        assert pushed == []


# ---------------------------------------------------------------------------
# T3: Live unknown still fires (regression guard)
# ---------------------------------------------------------------------------


class TestT3LiveUnknownStillFires:
    """A live non-suppressed terminal with WAITING frame must open episode + push."""

    def test_waiting_frame_pushes(self, monkeypatch, tmp_path):
        screen = _fixture("exited-terminal-residue.txt")
        provider = GrokLikeProvider()
        metadata = _metadata()
        backend = _backend(screen)
        _wire(monkeypatch, metadata, backend)
        monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path / "rules")
        monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path / "logs")
        pushed = []
        monkeypatch.setattr(ar.AutoResponder, "_push", lambda self, *args: pushed.append(args))

        engine = ar.AutoResponder()
        # NOT suppressed, NOT deleted — should fire
        result = engine.on_screen("term1", provider, screen)

        assert result == TerminalStatus.WAITING_USER_ANSWER
        assert len(pushed) == 1
        assert "unknown blocking dialog" in pushed[0][2]


# ---------------------------------------------------------------------------
# T4: Race — clear during on_screen
# ---------------------------------------------------------------------------


class TestT4RaceClearDuringOnScreen:
    """Generation bump via clear_terminal mid-scan prevents push."""

    def test_clear_after_should_push_blocks_push(self, monkeypatch, tmp_path):
        """Simulate: _check_unknown enters, clear_terminal fires, push is blocked."""
        screen = _fixture("exited-terminal-residue.txt")
        provider = GrokLikeProvider()
        metadata = _metadata()
        backend = _backend(screen)
        _wire(monkeypatch, metadata, backend)
        monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path / "rules")
        monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path / "logs")
        pushed = []

        engine = ar.AutoResponder()

        # Hook _push to track actual push attempts (use the real _run_fenced_effect)
        original_run_fenced = ar.AutoResponder._run_fenced_effect

        def tracking_push(self, terminal_id, meta, message, incarnation=None):
            pushed.append((terminal_id, message))

        monkeypatch.setattr(ar.AutoResponder, "_push", tracking_push)

        # First: do a normal call to verify push works
        result1 = engine.on_screen("term1", provider, screen)
        assert result1 == TerminalStatus.WAITING_USER_ANSWER
        assert len(pushed) == 1

        # Clear terminal (bumps generation from 0 → 1)
        engine.clear_terminal("term1")

        # Now simulate the race: a _check_unknown that captured incarnation
        # BEFORE the bump (gen=0) tries to push AFTER the bump (gen=1).
        # The Layer A recheck re-reads incarnation, gets gen=1, mismatches gen=0.
        # We test this by calling _check_unknown directly with a stale incarnation.
        region = ar.dialog_region(screen)
        stale_incarnation = (0, 7, "cao-sess", "win")  # pre-clear generation

        result = engine._check_unknown(
            "term1", metadata, "grok_cli", provider, screen, region,
            TerminalStatus.WAITING_USER_ANSWER, stale_incarnation,
        )
        # Episode opens (WAITING status returned) but no new push (incarnation mismatch)
        assert result == TerminalStatus.WAITING_USER_ANSWER
        assert len(pushed) == 1  # no new push


# ---------------------------------------------------------------------------
# T5: Delete-without-exit
# ---------------------------------------------------------------------------


class TestT5DeleteWithoutExit:
    """clear_terminal alone (no prior exit) suppresses further pushes for in-flight work."""

    def test_clear_terminal_blocks_inflight_push(self, monkeypatch, tmp_path):
        """An in-flight scan that captured a stale incarnation is blocked by Layer A."""
        screen = _fixture("exited-terminal-residue.txt")
        provider = GrokLikeProvider()
        metadata = _metadata()
        backend = _backend(screen)
        _wire(monkeypatch, metadata, backend)
        monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path / "rules")
        monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path / "logs")
        pushed = []
        monkeypatch.setattr(ar.AutoResponder, "_push", lambda self, *args: pushed.append(args))

        engine = ar.AutoResponder()

        # Snapshot incarnation before clear (gen=0)
        stale_incarnation = (0, 7, "cao-sess", "win")

        # Delete (clear_terminal) bumps generation to 1
        engine.clear_terminal("term1")

        # Simulate an in-flight _check_unknown that started before clear
        region = ar.dialog_region(screen)
        result = engine._check_unknown(
            "term1", metadata, "grok_cli", provider, screen, region,
            TerminalStatus.WAITING_USER_ANSWER, stale_incarnation,
        )
        # Episode opens but push is blocked (incarnation mismatch)
        assert result == TerminalStatus.WAITING_USER_ANSWER
        assert len(pushed) == 0


# ---------------------------------------------------------------------------
# T9: Rebind un-suppress
# ---------------------------------------------------------------------------


class TestT9RebindUnsuppress:
    """After exit + re-register, the terminal is no longer suppressed."""

    def test_register_terminal_clears_suppress(self, monkeypatch, tmp_path):
        """register_terminal un-suppresses so genuine dialogs can alert."""
        from cli_agent_orchestrator.services.auto_responder import auto_responder

        # Suppress first
        auto_responder.mark_exit_suppress("rebind-test")
        assert auto_responder.is_exit_suppressed("rebind-test")

        # Simulate register_terminal call (mock the delivery lock and internal registration)
        from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService

        svc = HerdrInboxService.__new__(HerdrInboxService)
        svc._identity_guard = threading.Lock()
        svc._terminal_to_pane = {}
        svc._pane_to_terminal = {}
        svc._kiro_terminals = set()
        svc._working_since = {}
        svc._terminal_identity = {}
        svc._pane_event_gen = {}

        # Mock internal locked registration and delivery lock
        monkeypatch.setattr(svc, "_register_terminal_locked", lambda *a, **k: None)
        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
            lambda tid: mock_lock,
        )

        svc.register_terminal("rebind-test", "pane1", False)

        assert not auto_responder.is_exit_suppressed("rebind-test")

    def test_register_under_guard_clears_suppress(self, monkeypatch, tmp_path):
        """_register_terminal_under_guard also un-suppresses."""
        from cli_agent_orchestrator.services.auto_responder import auto_responder

        auto_responder.mark_exit_suppress("rebind-test2")
        assert auto_responder.is_exit_suppressed("rebind-test2")

        from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService

        svc = HerdrInboxService.__new__(HerdrInboxService)
        svc._identity_guard = threading.Lock()
        svc._terminal_to_pane = {}
        svc._pane_to_terminal = {}
        svc._kiro_terminals = set()
        svc._working_since = {}
        svc._terminal_identity = {}
        svc._pane_event_gen = {}
        monkeypatch.setattr(svc, "_register_terminal_locked", lambda *a, **k: None)

        # Create a mock guard
        guard = MagicMock()
        guard.terminal_id = "rebind-test2"
        guard.active = True

        svc._register_terminal_under_guard("rebind-test2", "pane2", False, guard)

        assert not auto_responder.is_exit_suppressed("rebind-test2")

    def test_suppressed_then_rebind_then_alert(self, monkeypatch, tmp_path):
        """Full flow: exit → suppressed → rebind un-suppress → genuine dialog fires."""
        screen = _fixture("exited-terminal-residue.txt")
        provider = GrokLikeProvider()
        metadata = _metadata()
        backend = _backend(screen)
        _wire(monkeypatch, metadata, backend)
        monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path / "rules")
        monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path / "logs")
        pushed = []
        monkeypatch.setattr(ar.AutoResponder, "_push", lambda self, *args: pushed.append(args))

        engine = ar.AutoResponder()

        # Mark suppressed (exit)
        engine.mark_exit_suppress("term1")
        result = engine.on_screen("term1", provider, screen)
        assert result is None
        assert pushed == []

        # Un-suppress (rebind re-register)
        engine.unmark_exit_suppress("term1")

        # Now a genuine dialog should fire
        result2 = engine.on_screen("term1", provider, screen)
        assert result2 == TerminalStatus.WAITING_USER_ANSWER
        assert len(pushed) == 1
