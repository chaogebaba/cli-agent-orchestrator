"""F516 commit 3: D2 trust-order + settle-in-barrier + digest fields.

Option (B) staging (supervisor ruling): D2 removes the corroboration veto and
the classifier becomes fast-path-only. The D6 no-history HOLD / scroll-exclusion
lands in commit 6 — so in commits 3-5 an uncorroborated match FIRES (this is the
AC7 test_m3 red-window, expected and documented). Public-surface only.
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


def _wire(monkeypatch, metadata, backend):
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
        "cli_agent_orchestrator.services.seam_activation.receiver_state_active",
        lambda _op: False,
    )


def _backend(screen):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.capture_viewport.return_value = "\n".join(screen)
    return backend


def test_digest_domain_is_normalized_not_rows():
    """AC11(b): the two capture paths pad rows differently but normalize equal —
    digests over normalized must match for the same logical dialog."""
    pyte_shape = ["Choose working directory to resume this session", "  1. opt", "Press enter"]
    tmux_shape = [r + "        " for r in pyte_shape] + ["", "", ""]
    a = ar.dialog_region(pyte_shape)
    b = ar.dialog_region(tmux_shape)
    assert a.normalized == b.normalized
    assert ar._digest_normalized(a.normalized) == ar._digest_normalized(b.normalized)


def test_d2_fast_path_waiting_fires_on_first_eval(monkeypatch, tmp_path):
    """AC2(a)/D2: a WAITING-classified chooser fires on the first eval."""
    screen = DialogReplay("resume-chooser-61e1b848").final_rows()
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [RESUME_RULE])
    monkeypatch.setattr(ar.threading, "Thread", MagicMock())

    provider = _StatusProvider(TerminalStatus.WAITING_USER_ANSWER)
    assert ar.AutoResponder().on_screen("term1", provider, screen) is None
    backend.send_special_key.assert_called_once_with("cao-sess", "win", "Enter")


def test_ac11a_settle_mismatch_sends_no_keys(monkeypatch, tmp_path):
    """AC11(a): the frame changes between the rule-loop capture and the barrier
    capture → settle fails → zero keys."""
    rule_loop_screen = DialogReplay("resume-chooser-61e1b848").final_rows()
    barrier_screen = ["Choose working directory to resume this session", "DIFFERENT", "Press enter"]
    metadata = _metadata()
    backend = _backend(barrier_screen)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [RESUME_RULE])
    monkeypatch.setattr(ar.threading, "Thread", MagicMock())

    provider = _StatusProvider(TerminalStatus.WAITING_USER_ANSWER)
    assert ar.AutoResponder().on_screen("term1", provider, rule_loop_screen) is None
    backend.send_special_key.assert_not_called()
