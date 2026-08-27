"""F516 commit 4: auto_responder requests a D4 retry at the eval-end sites.

Option (B): in c4 the D6 no-history HOLD is not wired yet, so an uncorroborated
match fires (no HOLD retry to assert). The retry-request wiring exercised here
is busy-veto, wait-rule-active, and the open unknown-dialog episode.
"""

from unittest.mock import MagicMock

from test.helpers.dialog_replay import DialogReplay

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderCapabilities
from cli_agent_orchestrator.services import auto_responder as ar

RESUME_RULE = ar.Rule(
    "codex-resume-working-directory",
    True,
    "contains",
    "Choose working directory to resume this session",
    ["Press enter"],
    ["Enter"],
)


class _StatusProvider:
    capabilities = ProviderCapabilities(supports_screen_detection=True)

    def __init__(self, status):
        self.status = status

    def get_status_from_screen(self, _lines):
        return self.status


def _metadata(**overrides):
    md = {
        "id": "term1",
        "tmux_session": "cao-sess",
        "tmux_window": "win",
        "provider": "codex",
        "provider_session_id": None,
        "lifecycle_generation": 7,
    }
    md.update(overrides)
    return md


def _wire(monkeypatch, metadata, backend, retries):
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda tid: metadata if tid == metadata["id"] else None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_env.get_session_env", lambda _s: {}
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.list_terminals_by_session", lambda _s: []
    )
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.seam_activation.receiver_state_active", lambda _op: False
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.schedule_detection_retry",
        lambda tid, *a, **k: retries.append(tid),
    )
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [RESUME_RULE])
    monkeypatch.setattr(ar.threading, "Thread", MagicMock())


def _backend(screen):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.capture_viewport.return_value = "\n".join(screen)
    return backend


def test_busy_veto_on_matched_rule_requests_a_retry(monkeypatch, tmp_path):
    screen = DialogReplay("resume-chooser-61e1b848").final_rows()
    retries = []
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend, retries)
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    ar.AutoResponder().on_screen("term1", _StatusProvider(TerminalStatus.PROCESSING), screen)
    assert retries == ["term1"]
    backend.send_special_key.assert_not_called()


def test_wait_rule_active_requests_a_retry(monkeypatch, tmp_path):
    screen = ["please answer this", "waiting for you"]
    retries = []
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend, retries)
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    wait_rule = ar.Rule("w", True, "contains", "please answer", ["waiting"], "wait")
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [wait_rule])
    result = ar.AutoResponder().on_screen(
        "term1", _StatusProvider(TerminalStatus.WAITING_USER_ANSWER), screen
    )
    assert result == TerminalStatus.WAITING_USER_ANSWER
    assert retries == ["term1"]


def test_unknown_dialog_episode_requests_a_retry(monkeypatch, tmp_path):
    # A shaped-but-unmatched dialog with WAITING classifier opens an unknown
    # episode → WAITING result → retry requested.
    screen = ["1. Yes, continue", "2. No", "press enter to continue"]
    retries = []
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend, retries)
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [])  # no rule matches
    monkeypatch.setattr(ar.AutoResponder, "_push", lambda self, *a: None)
    result = ar.AutoResponder().on_screen(
        "term1", _StatusProvider(TerminalStatus.WAITING_USER_ANSWER), screen
    )
    assert result == TerminalStatus.WAITING_USER_ANSWER
    assert retries == ["term1"]
