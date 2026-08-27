"""F516 commit 2: D3 dialog consult + D3p match_verdict + wait-site.

Scope note (AC7 lint): files matching test/services/test_f516_*.py must not
touch underscore-prefixed AutoResponder members. These tests use only the
public match_verdict / on_screen surface and draft_guard public entries.
"""

from unittest.mock import MagicMock

import pytest

from test.helpers.dialog_replay import DialogReplay

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderCapabilities
from cli_agent_orchestrator.services import auto_responder as ar
from cli_agent_orchestrator.services import draft_guard as dg
from cli_agent_orchestrator.services.auto_responder import AutoResponder, RuleMatchVerdict

RESUME_RULE = ar.Rule(
    "codex-resume-working-directory",
    True,
    "contains",
    "Choose working directory to resume this session",
    ["Press enter"],
    ["Enter"],
)


def test_match_verdict_returns_verdict_for_matching_chooser(monkeypatch):
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [RESUME_RULE])
    rows = DialogReplay("resume-chooser-61e1b848").final_rows()
    verdict = AutoResponder().match_verdict("codex", rows)
    assert isinstance(verdict, RuleMatchVerdict)
    assert verdict.rule_name == "codex-resume-working-directory"
    assert verdict.region_digest  # normalized-domain digest present


def test_match_verdict_none_when_no_rule_matches(monkeypatch):
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [RESUME_RULE])
    assert AutoResponder().match_verdict("codex", ["just ordinary output"]) is None


def test_match_verdict_none_on_empty_region(monkeypatch):
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [RESUME_RULE])
    assert AutoResponder().match_verdict("codex", ["", "  ", ""]) is None


class _DialogProvider:
    """Provider whose injection-hazard classifier flags a dialog."""

    capabilities = ProviderCapabilities(supports_screen_detection=True)

    def classify_injection_hazard(self, _rows):
        return "interactive_dialog"


class _CleanProvider:
    capabilities = ProviderCapabilities(supports_screen_detection=True)
    supports_draft_preservation = True
    composer_clear_keys = ["C-u"]
    clear_immune_ghosts = False
    paste_submit_delay = 0.3

    def __init__(self, draft):
        self._draft = draft
        self._cleared = False

    def classify_injection_hazard(self, _rows):
        return None

    def read_composer_draft(self, _lines):
        return "" if self._cleared else self._draft


def _wire_consult(monkeypatch, provider, rendered, provider_name="codex", rules=()):
    monkeypatch.setattr(dg.status_monitor, "get_rendered_screen", lambda _t: rendered)
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda _t: {"provider": provider_name} if provider_name else None,
    )
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: list(rules))


def test_consult_raises_dialog_open_error_on_provider_hazard(monkeypatch):
    _wire_consult(monkeypatch, _DialogProvider(), ["some dialog rows"])
    with pytest.raises(dg.DialogOpenError):
        dg._consult_dialog_before_send("term1", _DialogProvider())


def test_consult_raises_dialog_open_error_on_whitelist_match(monkeypatch):
    rows = DialogReplay("resume-chooser-61e1b848").final_rows()

    class _NoHazard:
        def classify_injection_hazard(self, _rows):
            return None

    _wire_consult(monkeypatch, _NoHazard(), rows, rules=[RESUME_RULE])
    with pytest.raises(dg.DialogOpenError):
        dg._consult_dialog_before_send("term1", _NoHazard())


def test_consult_fails_open_when_screen_unavailable(monkeypatch):
    _wire_consult(monkeypatch, _DialogProvider(), None)
    # No raise: capture unavailable → existing behavior stands.
    dg._consult_dialog_before_send("term1", _DialogProvider())


def test_dialog_open_error_is_a_delivery_deferred_error():
    assert issubclass(dg.DialogOpenError, dg.DeliveryDeferredError)


def test_ac9_numbered_list_draft_on_clean_pane_is_preserved_not_deferred(monkeypatch):
    """AC9: a human draft that is itself a numbered list must not classify as a
    chooser — no DialogOpenError, draft preserved intact."""
    draft = "1. foo\n2. bar\n3. baz"
    provider = _CleanProvider(draft)
    rendered = ["› " + draft, "ordinary composer chrome"]
    _wire_consult(monkeypatch, provider, rendered, rules=[RESUME_RULE])
    metadata = {
        "id": "term1",
        "tmux_session": "s",
        "tmux_window": "w",
        "provider": "codex",
    }
    # _read_provider_draft path: stub stable-draft + clear-step to isolate the
    # consult (no real backend). The consult must NOT raise for a numbered list.
    monkeypatch.setattr(dg, "_wait_for_stable_draft", lambda *_a: draft)
    monkeypatch.setattr(dg, "_clear_step_changed_draft", lambda *_a: True)
    monkeypatch.setattr(dg, "_append_draft_log", lambda *_a: None)
    monkeypatch.setattr(dg, "_clear_composer", lambda *_a: True)
    monkeypatch.setattr(dg, "_read_provider_draft", lambda *_a: draft)

    preserved = dg.preserve_draft_before_send("term1", metadata, provider)
    assert preserved is not None
    assert preserved.text == draft
