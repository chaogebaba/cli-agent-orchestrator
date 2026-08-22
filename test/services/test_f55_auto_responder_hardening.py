"""F55 decision-wall tests for region, corroboration, lifecycle, and lock order."""

import ast
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderCapabilities
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
from cli_agent_orchestrator.services import auto_responder as ar

FIXTURES = Path(__file__).parents[1] / "fixtures" / "auto_responder"
CODEX_DIALOGS = Path(__file__).parents[1] / "fixtures" / "codex_dialogs"
REAL_THREAD = threading.Thread


class FixedStatusProvider:
    capabilities = ProviderCapabilities(supports_screen_detection=True)

    def __init__(self, status: TerminalStatus):
        self.status = status
        self.calls = 0

    def get_status_from_screen(self, _lines):
        self.calls += 1
        return self.status


def _metadata(**overrides):
    metadata = {
        "id": "term1",
        "tmux_session": "cao-sess",
        "tmux_window": "win",
        "provider": "codex",
        "provider_session_id": None,
        "lifecycle_generation": 7,
    }
    metadata.update(overrides)
    return metadata


def _wire(monkeypatch, metadata, backend, *, supervisors=()):
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


def _trust_rule() -> ar.Rule:
    return ar.Rule(
        "codex-trust-dir",
        True,
        "contains",
        "Do you trust the contents of this directory?",
        ["Yes, continue", "No, quit"],
        ["Enter"],
    )


def _fixture(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def test_dialog_region_strips_only_trailing_blank_rows_and_preserves_rendered_rows():
    screen = ["header", "", "dialog row  ", "option", "   ", ""]

    region = ar.dialog_region(screen)

    assert region.rows == ("header", "", "dialog row  ", "option")
    assert region.normalized == "header dialog row option"


def test_all_rule_match_call_sites_receive_a_dialog_region_normalized_value():
    source = (
        Path(__file__).parents[2] / "src/cli_agent_orchestrator/services/auto_responder.py"
    ).read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "matches"
    ]

    assert len(calls) == 5
    assert all(
        len(call.args) == 1
        and isinstance(call.args[0], ast.Attribute)
        and call.args[0].attr == "normalized"
        for call in calls
    )


def test_incident_variant2_is_suppressed_independently_by_region_and_busy_veto(
    monkeypatch, tmp_path
):
    screen = _fixture("incident-variant2.txt")
    provider = CodexProvider("term1", "cao-sess", "win")
    rule = _trust_rule()
    pinned_rows = tuple(screen[-ar.DIALOG_REGION_LINES :])
    region = ar.DialogRegion(pinned_rows, ar.normalize_screen(list(pinned_rows)))
    sent = []
    pushed = []
    metadata = _metadata()
    backend = _backend(screen)
    backend.send_special_key.side_effect = lambda *args: sent.append(args)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path / "rules")
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ar.AutoResponder, "_push", lambda self, *args: pushed.append(args))
    engine = ar.AutoResponder()

    assert rule.matches(ar.normalize_screen(screen))
    assert not rule.matches(region.normalized)
    assert provider.get_status_from_screen(list(region.rows)) == TerminalStatus.PROCESSING
    assert engine.on_screen("term1", provider, screen) is None
    assert sent == []
    assert pushed == []


def test_incident_base_remains_a_never_match_control():
    screen = _fixture("incident-base.txt")
    rule = _trust_rule()

    assert not rule.matches(ar.normalize_screen(screen))
    assert not rule.matches(ar.dialog_region(screen).normalized)


def test_m1_variant2_bottom_is_region_matched_processing_and_does_not_fire(monkeypatch, tmp_path):
    screen = _fixture("variant2-bottom.txt")
    provider = CodexProvider("term1", "cao-sess", "win")
    rule = _trust_rule()
    region = ar.dialog_region(screen)
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path / "rules")
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ar.threading, "Thread", MagicMock())

    assert rule.matches(region.normalized)
    assert ar.AutoResponder._has_dialog_proximity(region.normalized)
    assert provider.get_status_from_screen(list(region.rows)) == TerminalStatus.PROCESSING
    assert ar.AutoResponder().on_screen("term1", provider, screen) is None
    backend.send_special_key.assert_not_called()


def test_m2_variant2_minus_spinner_is_suppressed_only_by_region(monkeypatch, tmp_path):
    screen = _fixture("variant2-minus-spinner.txt")
    provider = CodexProvider("term1", "cao-sess", "win")
    rule = _trust_rule()
    pinned_rows = tuple(screen[-ar.DIALOG_REGION_LINES :])
    region = ar.DialogRegion(pinned_rows, ar.normalize_screen(list(pinned_rows)))
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path / "rules")
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ar.threading, "Thread", MagicMock())

    assert rule.matches(ar.normalize_screen(screen))
    assert not rule.matches(region.normalized)
    assert provider.get_status_from_screen(list(region.rows)) == TerminalStatus.IDLE
    assert ar.AutoResponder().on_screen("term1", provider, screen) is None
    backend.send_special_key.assert_not_called()


def test_m3_matching_options_content_without_corroboration_does_not_fire(monkeypatch):
    screen = [
        "Do you trust the contents of this directory?",
        "Yes, continue",
        "No, quit",
    ]
    provider = FixedStatusProvider(TerminalStatus.UNKNOWN)
    metadata = _metadata(provider="test")
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar._store, "get_rules", lambda _provider: [_trust_rule()])

    assert not ar.AutoResponder._has_dialog_proximity(ar.dialog_region(screen).normalized)
    assert ar.AutoResponder().on_screen("term1", provider, screen) is None
    backend.send_special_key.assert_not_called()


def test_real_seed_trust_v2_modal_is_region_waiting_and_fires(monkeypatch, tmp_path):
    screen = (CODEX_DIALOGS / "trust.ansi.txt").read_text(encoding="utf-8").splitlines()
    provider = CodexProvider("term1", "cao-sess", "win")
    metadata = _metadata()
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path / "rules")
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ar.threading, "Thread", MagicMock())
    ar._store._cache.clear()
    region = ar.dialog_region(screen)

    assert len(screen) == 40
    assert len(region.rows) == 9
    assert provider.get_status_from_screen(list(region.rows)) == TerminalStatus.WAITING_USER_ANSWER
    assert ar.AutoResponder().on_screen("term1", provider, screen) is None
    backend.send_special_key.assert_called_once_with("cao-sess", "win", "Enter")


def _grok_picker() -> list[str]:
    return """
  ┃  Run Grok Build in a project directory?
  ┃
  ┃  1 (○) minimal (current)   /tmp/cao-grok-probe.AlB5Ka/minimal
  ┃  z (○) Type your answer here
  ┃
  ┃  ↑/↓ navigate · y copy                                    Enter:submit
""".splitlines()


def test_grok_picker_waiting_arm_drives_unknown_push_without_generic_proximity(monkeypatch):
    screen = _grok_picker()
    provider = GrokCliProvider("term1", "cao-sess", "win")
    metadata = _metadata(provider="grok_cli")
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar._store, "get_rules", lambda _provider: [])
    pushed = []
    engine = ar.AutoResponder()
    monkeypatch.setattr(engine, "_push", lambda *args: pushed.append(args))

    assert not engine._has_dialog_proximity(ar.dialog_region(screen).normalized)
    assert provider.get_status_from_screen(screen) == TerminalStatus.WAITING_USER_ANSWER
    assert engine.on_screen("term1", provider, screen) == TerminalStatus.WAITING_USER_ANSWER
    assert len(pushed) == 1


def test_grok_picker_test_local_fire_rule_uses_waiting_arm_and_region(monkeypatch, tmp_path):
    screen = _grok_picker()
    provider = GrokCliProvider("term1", "cao-sess", "win")
    metadata = _metadata(provider="grok_cli")
    backend = _backend(screen)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path)
    monkeypatch.setattr(ar.threading, "Thread", MagicMock())
    rule = ar.Rule(
        "grok-project-picker",
        True,
        "contains",
        "Run Grok Build in a project directory?",
        ["minimal (current)", "Type your answer here"],
        ["Enter"],
    )
    monkeypatch.setattr(ar._store, "get_rules", lambda _provider: [rule])

    assert not ar.AutoResponder._has_dialog_proximity(ar.dialog_region(screen).normalized)
    assert ar.AutoResponder().on_screen("term1", provider, screen) is None
    backend.send_special_key.assert_called_once_with("cao-sess", "win", "Enter")

    backend.reset_mock()
    mid_scrollback = (
        [f"old row {index}" for index in range(20)]
        + screen
        + [f"new row {index}" for index in range(20)]
    )
    backend.capture_viewport.return_value = "\n".join(mid_scrollback)
    assert ar.AutoResponder().on_screen("term1", provider, mid_scrollback) is None
    backend.send_special_key.assert_not_called()


def test_supplied_frame_is_classified_exactly_once_per_tick(monkeypatch):
    provider = FixedStatusProvider(TerminalStatus.IDLE)
    metadata = _metadata(provider="test")
    backend = _backend(["ordinary output"])
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar._store, "get_rules", lambda _provider: [])

    assert ar.AutoResponder().on_screen("term1", provider, ["ordinary output"]) is None
    assert provider.calls == 1


def test_m5_retry_processing_reenforcement_aborts_without_send_or_push(monkeypatch):
    cached = ar.dialog_region(["trust ok"])
    fresh = _fixture("variant2-bottom.txt")
    provider = CodexProvider("term1", "cao-sess", "win")
    metadata = _metadata()
    backend = _backend(fresh)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(ar.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        ar.AutoResponder, "_current_normalized", staticmethod(lambda _terminal: cached)
    )
    pushed = []
    engine = ar.AutoResponder()
    monkeypatch.setattr(engine, "_push", lambda *args: pushed.append(args))
    rule = ar.Rule("r", True, "contains", "trust", ["ok"], ["Enter"])
    incarnation = engine._snapshot_incarnation("term1", metadata)

    engine._verify_and_retry(
        "term1", metadata, provider, rule, engine._state_for("term1", "r"), incarnation
    )

    backend.send_special_key.assert_not_called()
    assert pushed == []


def test_clear_terminal_bumps_generation_and_clears_cooldowns_and_waiting_state():
    engine = ar.AutoResponder()
    engine._rule_state[("term1", "r1")] = ar._RuleState(cooldown_until=99)
    engine._rule_state[("other", "r2")] = ar._RuleState(cooldown_until=99)
    engine._wait_rule_active["term1"] = ("wait", 1.0)
    engine._retry_exhausted.add("term1")
    engine._unknown_state["term1"] = ar._UnknownDialogState(episode_open=True)

    engine.clear_terminal("term1")

    assert engine._terminal_generation["term1"] == 1
    assert ("term1", "r1") not in engine._rule_state
    assert ("other", "r2") in engine._rule_state
    assert engine.waiting_gate("term1") is None


def test_delivery_lock_is_non_reentrant_but_clear_terminal_is_lock_free():
    from cli_agent_orchestrator.services.inbox_service import get_delivery_lock

    engine = ar.AutoResponder()
    lock = get_delivery_lock("f55-lock-order")
    clear_finished = threading.Event()

    def clear_terminal():
        engine.clear_terminal("f55-lock-order")
        clear_finished.set()

    assert lock.acquire(blocking=False)
    try:
        assert not lock.acquire(blocking=False)
        worker = REAL_THREAD(target=clear_terminal)
        worker.start()
        finished_without_delivery_lock = clear_finished.wait(0.5)
    finally:
        lock.release()
    worker.join(1)

    assert finished_without_delivery_lock, (
        "clear_terminal blocked on the non-reentrant delivery lock; "
        "the delete path would deadlock"
    )
    assert not worker.is_alive()
    assert engine._terminal_generation["f55-lock-order"] == 1


def test_incarnation_fence_rejects_rebound_and_coordinate_changes(monkeypatch):
    metadata = _metadata()
    current = dict(metadata)
    backend = _backend(["trust ok"])
    _wire(monkeypatch, current, backend)
    engine = ar.AutoResponder()
    rule = ar.Rule("r", True, "contains", "trust", ["ok"], ["Enter"])
    incarnation = engine._snapshot_incarnation("term1", metadata)

    current["lifecycle_generation"] += 1
    assert not engine._send_answer("term1", metadata, rule, incarnation)
    current["lifecycle_generation"] -= 1
    current["tmux_window"] = "rebound-window"
    assert not engine._send_answer("term1", metadata, rule, incarnation)
    current["tmux_window"] = metadata["tmux_window"]
    current["tmux_session"] = "rebound-session"
    assert not engine._send_answer("term1", metadata, rule, incarnation)
    backend.send_special_key.assert_not_called()


def test_d4_a1_sequence_keeps_screen_barrier_at_sequence_start(monkeypatch):
    class FrameStatusProvider:
        def get_status_from_screen(self, lines):
            return (
                TerminalStatus.PROCESSING
                if "processing" in ar.normalize_screen(lines)
                else TerminalStatus.WAITING_USER_ANSWER
            )

    metadata = _metadata()
    current = dict(metadata)
    backend = _backend(["trust ok"])
    sent_keys = []

    def send_key(_session, _window, key):
        sent_keys.append(key)
        if key == "A":
            backend.capture_viewport.return_value = "processing"

    backend.send_special_key.side_effect = send_key
    _wire(monkeypatch, current, backend)
    monkeypatch.setattr(ar.threading, "Thread", MagicMock())
    engine = ar.AutoResponder()
    monkeypatch.setattr(engine, "_log", lambda *_args: None)
    real_barrier = engine._effect_barrier
    barrier_calls = 0

    def counted_barrier(*args):
        nonlocal barrier_calls
        barrier_calls += 1
        return real_barrier(*args)

    monkeypatch.setattr(engine, "_effect_barrier", counted_barrier)
    rule = ar.Rule("r", True, "contains", "trust", ["ok"], ["A", "B"])
    incarnation = engine._snapshot_incarnation("term1", metadata)

    assert engine._fire(
        "term1",
        metadata,
        FrameStatusProvider(),
        rule,
        "trust ok",
        ar._RuleState(),
        incarnation,
    )

    assert sent_keys == ["A", "B"]
    assert backend.capture_viewport.return_value == "processing"
    assert barrier_calls == 1


def test_d4_a1_sequence_rechecks_incarnation_before_each_key(monkeypatch):
    metadata = _metadata()
    current = dict(metadata)
    backend = _backend(["trust ok"])

    def rebind_after_first_key(_session, _window, key):
        assert key == "A"
        current["tmux_session"] = "rebound-session"

    backend.send_special_key.side_effect = rebind_after_first_key
    _wire(monkeypatch, current, backend)
    monkeypatch.setattr(ar.time, "sleep", lambda _seconds: None)
    engine = ar.AutoResponder()
    rule = ar.Rule("r", True, "contains", "trust", ["ok"], ["A", "B"])
    incarnation = engine._snapshot_incarnation("term1", metadata)

    assert not engine._send_answer("term1", metadata, rule, incarnation)
    backend.send_special_key.assert_called_once_with("cao-sess", "win", "A")


def test_post_clear_push_and_retry_token_are_dropped(monkeypatch):
    metadata = _metadata()
    row = {"value": metadata}
    backend = _backend(["trust ok"])
    _wire(
        monkeypatch,
        metadata,
        backend,
        supervisors=[{"id": "sup1", "provider": "claude_code"}],
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda terminal_id: row["value"] if terminal_id == "term1" else None,
    )
    inserted = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        lambda *args: inserted.append(args),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(ar.time, "sleep", lambda _seconds: None)
    engine = ar.AutoResponder()
    incarnation = engine._snapshot_incarnation("term1", metadata)
    engine.clear_terminal("term1")
    row["value"] = None

    engine._push("term1", metadata, "unknown origin", incarnation)
    engine._verify_and_retry(
        "term1",
        metadata,
        FixedStatusProvider(TerminalStatus.WAITING_USER_ANSWER),
        ar.Rule("r", True, "contains", "trust", ["ok"], ["Enter"]),
        ar._RuleState(),
        incarnation,
    )

    assert inserted == []
    backend.send_special_key.assert_not_called()


def test_m4_effect_and_real_delete_terminal_are_serialized_by_delivery_lock(monkeypatch, tmp_path):
    from cli_agent_orchestrator.services import terminal_service

    metadata = _metadata()
    row = {"value": metadata}
    order = []
    send_entered = threading.Event()
    release_send = threading.Event()
    delete_done = threading.Event()
    backend = _backend(["trust ok"])

    def blocked_send(*_args):
        order.append("send-start")
        send_entered.set()
        assert release_send.wait(2)
        order.append("send-end")

    backend.send_special_key.side_effect = blocked_send
    backend.get_history.return_value = ""
    backend.get_pane_working_directory.return_value = str(tmp_path)
    _wire(monkeypatch, metadata, backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda terminal_id: row["value"] if terminal_id == "term1" else None,
    )
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _terminal: row["value"])
    monkeypatch.setattr(terminal_service, "quiesce_deferred_terminal_sync", lambda _tid: None)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
        lambda _tid: object(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease",
        lambda _token: None,
    )
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)
    monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", tmp_path)
    monkeypatch.setattr(terminal_service, "fifo_manager", MagicMock())
    monkeypatch.setattr(terminal_service, "status_monitor", MagicMock())
    monkeypatch.setattr(terminal_service, "provider_manager", MagicMock())
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.persona_context.cleanup_persona", lambda _tid: None
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog.clear_terminal",
        lambda _tid: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.clear_terminal_delivery_state",
        lambda _tid: None,
    )
    monkeypatch.setattr(terminal_service, "dispatch_plugin_event", lambda *_args: None)

    def delete_row(_terminal_id, **_kwargs):
        order.append("db-delete")
        row["value"] = None
        return {"terminal_deleted": True, "intent_deleted": True}

    monkeypatch.setattr(terminal_service, "delete_terminal_and_warm_intent", delete_row)
    monkeypatch.setattr(ar, "AUTO_ANSWER_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ar.threading, "Thread", MagicMock())
    monkeypatch.setattr(
        ar._store,
        "get_rules",
        lambda _provider: [ar.Rule("r", True, "contains", "trust", ["ok"], ["Enter"])],
    )
    engine = ar.AutoResponder()
    original_clear = engine.clear_terminal

    def ordered_clear(terminal_id):
        order.append("clear")
        original_clear(terminal_id)

    monkeypatch.setattr(engine, "clear_terminal", ordered_clear)
    monkeypatch.setattr(ar, "auto_responder", engine)

    responder = REAL_THREAD(
        target=lambda: engine.on_screen(
            "term1", FixedStatusProvider(TerminalStatus.WAITING_USER_ANSWER), ["trust ok"]
        )
    )
    responder.start()
    assert send_entered.wait(1)

    def delete():
        terminal_service.delete_terminal("term1")
        delete_done.set()

    deleting = REAL_THREAD(target=delete)
    deleting.start()
    time.sleep(0.05)
    assert not delete_done.is_set()
    release_send.set()
    responder.join(2)
    deleting.join(2)

    assert not responder.is_alive()
    assert not deleting.is_alive()
    assert delete_done.is_set()
    # Layer C (F115) adds an early idempotent clear at _delete_terminal_under_lease
    # start (before any lock), so "clear" may appear before "send-end". The key
    # serialization invariant is: the LATE clear (under delivery lock) and db-delete
    # both follow send-end. With two clears, check the last clear is properly ordered.
    clear_indices = [i for i, v in enumerate(order) if v == "clear"]
    assert len(clear_indices) >= 1
    last_clear = clear_indices[-1]
    assert order.index("send-end") < last_clear < order.index("db-delete")
    # Generation bumps: early clear (1) + late clear under lock (2)
    assert engine._terminal_generation["term1"] == 2
