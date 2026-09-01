"""F700 #555: ``modality: hard`` rules bypass the busy veto.

Trust-class modals (codex hooks-trust, codex/grok trust-directory) render while
a spinner/work marker is also on screen, so the provider's single-axis classifier
returns PROCESSING and ``_busy_veto`` suppresses a rule that DID match — the
"matched but was vetoed 5+ evals" streak in #555. A rule declared
``modality: hard`` asserts that its own match proves the CLI is input-blocked,
so the veto does not apply to it. Soft rules (the default) are unchanged.

MUTANT: delete the ``rule.is_hard`` check at either ``_busy_veto`` call site in
``auto_responder.py`` and ``test_hard_rule_fires_under_working_status`` fails
(zero keys sent, decision log reason=busy_veto).
"""

from test.helpers.dialog_replay import DialogReplay
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderCapabilities
from cli_agent_orchestrator.services import auto_responder as ar
from cli_agent_orchestrator.services.auto_responder import AutoResponder

QUESTION = "Choose working directory to resume this session"


def _rule(modality):
    return ar.Rule(
        "codex-resume-working-directory",
        True,
        "contains",
        QUESTION,
        ["Press enter"],
        ["Enter"],
        modality,
    )


class _StatusProvider:
    capabilities = ProviderCapabilities(supports_screen_detection=True)

    def __init__(self, status):
        self.status = status

    def get_status_from_screen(self, _lines):
        return self.status


def _metadata():
    return {
        "id": "term1",
        "tmux_session": "cao-sess",
        "tmux_window": "win",
        "provider": "codex",
        "provider_session_id": None,
        "lifecycle_generation": 7,
    }


def _backend(screen):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.capture_viewport.return_value = "\n".join(screen)
    return backend


def _wire(monkeypatch, metadata, backend, rule):
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
        lambda tid, *a, **k: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
        lambda tid, *a, **k: list(backend.capture_viewport.return_value.splitlines()),
    )
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [rule])
    monkeypatch.setattr(ar.threading, "Thread", MagicMock())
    monkeypatch.setattr(ar, "_clock_sleep", lambda _s: None)


def _run_two_evals(monkeypatch, tmp_path, modality):
    """Two evals of one static matching frame under a WORKING (PROCESSING)
    classifier. Two evals so region history exists and the D6 no-history HOLD
    (orthogonal to the busy veto) does not decide the outcome."""
    screen = DialogReplay("resume-chooser-61e1b848").final_rows()
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend, _rule(modality))
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    engine = AutoResponder()
    provider = _StatusProvider(TerminalStatus.PROCESSING)
    engine.on_screen("term1", provider, screen)
    engine.on_screen("term1", provider, screen)
    log = (tmp_path / "term1.decisions.log").read_text()
    return backend, log


def test_hard_rule_fires_under_working_status(monkeypatch, tmp_path):
    """A matching ``modality: hard`` rule sends its keys even though the
    classifier reports PROCESSING, and the bypass is recorded in the log."""
    backend, log = _run_two_evals(monkeypatch, tmp_path, "hard")

    backend.send_special_key.assert_called_once_with("cao-sess", "win", "Enter")
    assert "reason=modality_hard" in log
    assert "modality=hard busy_bypassed=True" in log
    assert "reason=busy_veto" not in log


def test_soft_rule_is_vetoed_under_the_same_status(monkeypatch, tmp_path):
    """The same rule declared soft (the default) is still vetoed by PROCESSING:
    zero keys, and the veto is the logged reason."""
    backend, log = _run_two_evals(monkeypatch, tmp_path, "soft")

    backend.send_special_key.assert_not_called()
    assert "reason=busy_veto" in log
    assert "modality=hard" not in log


def test_default_modality_is_soft():
    assert _rule("soft").modality == "soft"
    assert not ar.Rule("r", True, "contains", "q", [], ["Enter"]).is_hard


@pytest.mark.parametrize("bad", ["HARD", "blocking", "", None, 1])
def test_unknown_modality_degrades_to_soft(bad):
    """An unrecognized value must not silently arm a veto bypass."""
    rule = ar.Rule("r", True, "contains", "q", [], ["Enter"], bad)
    assert rule.modality == "soft"
    assert not rule.is_hard


def test_loader_reads_modality_from_yaml(tmp_path, monkeypatch):
    path = tmp_path / "codex.yaml"
    path.write_text(
        "- name: hard-one\n"
        "  question: q1\n"
        "  answer: [Enter]\n"
        "  modality: hard\n"
        "- name: soft-one\n"
        "  question: q2\n"
        "  answer: [Enter]\n",
        encoding="utf-8",
    )
    rules = ar._RuleStore._load(path)
    assert [(r.name, r.is_hard) for r in rules] == [("hard-one", True), ("soft-one", False)]
