"""F516 commit 6: D6 region-history + scroll-exclusion + veto-streak + banner path.

Commit 6 introduces the no-history HOLD (test_m3 goes GREEN here) and the full
D6 machinery. Named outside the test_f516_* AC7-lint scope (drives internals).
"""

from unittest.mock import MagicMock

from test.helpers.dialog_replay import DialogReplay

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderCapabilities
from cli_agent_orchestrator.services import auto_responder as ar
from cli_agent_orchestrator.services.auto_responder import AutoResponder

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


def _wire(monkeypatch, metadata, backend, retries=None):
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
        lambda tid, *a, **k: (retries.append(tid) if retries is not None else None),
    )
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [RESUME_RULE])
    monkeypatch.setattr(ar.threading, "Thread", MagicMock())


def _backend(screen):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.capture_viewport.return_value = "\n".join(screen)
    return backend


def test_ac2a_f509_shape_holds_first_eval_then_fires_on_static_re_eval(monkeypatch, tmp_path):
    """AC2(a): a chooser with the classifier forced IDLE HOLDs on eval 1 (no
    history) and fires on eval 2 (history present, region static → eligible)."""
    screen = DialogReplay("resume-chooser-61e1b848").final_rows()
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    engine = AutoResponder()
    provider = _StatusProvider(TerminalStatus.IDLE)

    assert engine.on_screen("term1", provider, screen) is None
    backend.send_special_key.assert_not_called()
    assert engine.on_screen("term1", provider, screen) is None
    backend.send_special_key.assert_called_once_with("cao-sess", "win", "Enter")


def test_ac2b_scrolling_banner_is_suppressed_zero_keys(monkeypatch, tmp_path):
    """AC2(b): a matched rule over a still-scrolling banner region sends no keys."""
    replay = DialogReplay("content-policy-banner-f02ce13e")
    frame_a, frame_b = replay.rows_at(0), replay.rows_at(1)
    metadata = _metadata()
    backend = _backend(frame_b)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    from cli_agent_orchestrator.services.auto_responder import dialog_region

    reg_b = dialog_region(frame_b)
    token = reg_b.normalized.split()[0] if reg_b.normalized.split() else "x"
    banner_rule = ar.Rule("banner", True, "contains", token, [], ["Enter"])
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [banner_rule])
    engine = AutoResponder()
    provider = _StatusProvider(TerminalStatus.IDLE)

    engine.on_screen("term1", provider, frame_a)
    engine.on_screen("term1", provider, frame_b)
    backend.send_special_key.assert_not_called()


def test_match_verdict_banner_path_moving_returns_none(monkeypatch):
    """D6: a match over a still-moving banner-marked region → match_verdict None;
    static → returns the verdict."""
    replay = DialogReplay("content-policy-banner-f02ce13e")
    frame_a, frame_b = replay.rows_at(0), replay.rows_at(1)
    from cli_agent_orchestrator.services.auto_responder import dialog_region

    reg_b = dialog_region(frame_b)
    token = reg_b.normalized.split()[0] if reg_b.normalized.split() else "x"
    banner_rule = ar.Rule("banner", True, "contains", token, [], ["Enter"])
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [banner_rule])
    engine = AutoResponder()

    engine._push_region_history("term1", dialog_region(frame_a))
    engine._push_region_history("term1", reg_b)
    moving_lines = replay.rows_at(2)
    assert engine.match_verdict("codex", moving_lines, terminal_id="term1") is None
    v = engine.match_verdict("codex", frame_b, terminal_id="term1")
    assert v is not None and v.rule_name == "banner"


def test_veto_streak_pushes_once_within_bound(monkeypatch, tmp_path):
    """AC6 shape: classifier PROCESSING over a matching chooser (busy-veto) —
    ≥5 consecutive vetoed evals emit exactly one push per episode."""
    screen = DialogReplay("resume-chooser-61e1b848").final_rows()
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    engine = AutoResponder()
    pushes = []
    monkeypatch.setattr(engine, "_push", lambda *a: pushes.append(a))
    provider = _StatusProvider(TerminalStatus.PROCESSING)

    for _ in range(6):
        engine.on_screen("term1", provider, screen)
    assert len(pushes) == 1
    backend.send_special_key.assert_not_called()


def test_clear_terminal_purges_d6_state():
    engine = AutoResponder()
    from cli_agent_orchestrator.services.auto_responder import dialog_region

    engine._push_region_history("term1", dialog_region(["a", "b"]))
    engine._note_veto_streak("term1", _metadata(), ("g", 7, "s", "w"))
    engine.clear_terminal("term1")
    assert "term1" not in engine._region_history
    assert "term1" not in engine._prefilter_verdict
    assert "term1" not in engine._veto_streak


def test_ac2b_delivery_arm_scrolling_banner_does_not_raise_dialog_open_error(monkeypatch):
    """AC2(b) delivery arm (r5-B2): the banner fixture on the draft_guard consult
    path does NOT raise DialogOpenError while still-scrolling."""
    from cli_agent_orchestrator.services import draft_guard as dg
    from cli_agent_orchestrator.services.auto_responder import auto_responder, dialog_region

    replay = DialogReplay("content-policy-banner-f02ce13e")
    frame_a, frame_b, frame_c = replay.rows_at(0), replay.rows_at(1), replay.rows_at(2)
    reg_b = dialog_region(frame_b)
    token = reg_b.normalized.split()[0] if reg_b.normalized.split() else "x"
    banner_rule = ar.Rule("banner", True, "contains", token, [], ["Enter"])
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [banner_rule])

    auto_responder.clear_terminal("term-banner")
    auto_responder._push_region_history("term-banner", dialog_region(frame_a))
    auto_responder._push_region_history("term-banner", reg_b)

    class _NoHazard:
        def classify_injection_hazard(self, _rows):
            return None

    monkeypatch.setattr(dg.status_monitor, "get_rendered_screen", lambda _t: frame_c)
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda _t: {"provider": "codex"},
    )
    dg._consult_dialog_before_send("term-banner", _NoHazard())
    auto_responder.clear_terminal("term-banner")
