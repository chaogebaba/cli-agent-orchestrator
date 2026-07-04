"""Tests for the whitelist-only auto-responder engine."""

import threading
import time

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import auto_responder as ar


class FakeProvider:
    supports_screen_detection = True


class SyncThread:
    """Drop-in for threading.Thread that runs the target synchronously.

    Keeps the retry/verify tests deterministic instead of racing a real
    background thread.
    """

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture(autouse=True)
def _reset_engine(monkeypatch):
    """Fresh engine + rule cache + no real sleeping for every test."""
    monkeypatch.setattr(ar.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ar.threading, "Thread", SyncThread)
    engine = ar.AutoResponder()
    monkeypatch.setattr(ar, "auto_responder", engine)
    ar._store._cache.clear()
    yield engine


def _metadata(**overrides):
    base = {
        "id": "term1",
        "tmux_session": "cao-sess",
        "tmux_window": "win",
        "provider": "codex",
    }
    base.update(overrides)
    return base


def _wire_common(monkeypatch, metadata=None, session_env=None, sent_keys=None):
    metadata = metadata or _metadata()
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda tid: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_env.get_session_env",
        lambda session: session_env or {},
    )

    class FakeBackend:
        def send_special_key(self, session, window, key):
            (sent_keys if sent_keys is not None else []).append((session, window, key))

    monkeypatch.setattr(
        "cli_agent_orchestrator.backends.registry.get_backend", lambda: FakeBackend()
    )

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.list_terminals_by_session",
        lambda session: [],
    )
    return metadata


# ----- normalization / line-break trap ------------------------------------


def test_normalize_screen_collapses_wrapped_newlines():
    wrapped = ["Do you trust the", "contents of this directory?"]
    unwrapped = ["Do you trust the contents of this directory?"]
    assert ar.normalize_screen(wrapped) == ar.normalize_screen(unwrapped)
    assert ar.normalize_screen(wrapped) == "Do you trust the contents of this directory?"


def test_normalize_screen_collapses_trailing_pad_spaces():
    # pyte pads each line to the screen width with spaces
    padded = ["Yes, continue    ", "   No, quit          "]
    assert ar.normalize_screen(padded) == "Yes, continue No, quit"


# ----- rule matching --------------------------------------------------------


def test_contains_match_requires_question_and_all_options():
    rule = ar.Rule(
        name="r",
        enabled=True,
        match_mode="contains",
        question="Do you trust the contents of this directory?",
        options=["Yes, continue", "No, quit"],
        answer=["Enter"],
    )
    assert rule.matches("Do you trust the contents of this directory? 1. Yes, continue 2. No, quit")
    assert not rule.matches("Do you trust the contents of this directory? 1. Yes, continue")
    assert not rule.matches("Some unrelated screen")


def test_regex_match_masks_variable_parts():
    rule = ar.Rule(
        name="r",
        enabled=True,
        match_mode="regex",
        question=r"You have \d+ usage limit resets available",
        options=["Yes, continue", "No, quit"],
        answer=["Enter"],
    )
    assert rule.matches("You have 3 usage limit resets available Yes, continue No, quit")
    assert rule.matches("You have 12 usage limit resets available Yes, continue No, quit")
    assert not rule.matches("You have usage limit resets available Yes, continue No, quit")


def test_disabled_rule_never_matches():
    rule = ar.Rule(
        name="r",
        enabled=False,
        match_mode="contains",
        question="trust",
        options=[],
        answer=["Enter"],
    )
    assert not rule.matches("trust this directory")


# ----- engine: firing, retry, cooldown -------------------------------------


def test_matched_rule_fires_answer_keys(monkeypatch, _reset_engine):
    sent = []
    metadata = _wire_common(monkeypatch, sent_keys=sent)
    monkeypatch.setattr(
        ar._store,
        "get_rules",
        lambda provider: [
            ar.Rule(
                "codex-trust-dir", True, "contains", "Do you trust", ["Yes, continue"], ["Enter"]
            )
        ],
    )
    # dismiss on first check so the background retry thread returns immediately
    monkeypatch.setattr(ar.AutoResponder, "_current_normalized", staticmethod(lambda tid: ""))

    result = _reset_engine.on_screen("term1", FakeProvider(), ["Do you trust", "Yes, continue"])
    assert result is None  # firing doesn't override — falls through to normal detection
    assert sent == [("cao-sess", "win", "Enter")]


def test_cooldown_suppresses_immediate_refire(monkeypatch, _reset_engine):
    sent = []
    _wire_common(monkeypatch, sent_keys=sent)
    monkeypatch.setattr(
        ar._store,
        "get_rules",
        lambda provider: [ar.Rule("r", True, "contains", "trust", ["ok"], ["Enter"])],
    )
    monkeypatch.setattr(ar.AutoResponder, "_current_normalized", staticmethod(lambda tid: ""))

    _reset_engine.on_screen("term1", FakeProvider(), ["trust ok"])
    assert len(sent) == 1

    # Same dialog still on screen a moment later (redraw) — cooldown blocks refire.
    _reset_engine.on_screen("term1", FakeProvider(), ["trust ok"])
    assert len(sent) == 1


def test_retry_stops_once_dialog_dismissed(monkeypatch, _reset_engine):
    sent = []
    _wire_common(monkeypatch, sent_keys=sent)
    monkeypatch.setattr(
        ar._store,
        "get_rules",
        lambda provider: [ar.Rule("r", True, "contains", "trust", ["ok"], ["Enter"])],
    )
    # First recheck already shows the dialog gone.
    monkeypatch.setattr(ar.AutoResponder, "_current_normalized", staticmethod(lambda tid: "gone"))

    _reset_engine.on_screen("term1", FakeProvider(), ["trust ok"])
    assert len(sent) == 1  # only the initial fire, no retries


def test_retry_cap_surfaces_waiting_and_pushes(monkeypatch, _reset_engine):
    sent = []
    metadata = _wire_common(monkeypatch, sent_keys=sent)
    monkeypatch.setattr(
        ar._store,
        "get_rules",
        lambda provider: [ar.Rule("r", True, "contains", "trust", ["ok"], ["Enter"])],
    )
    # Dialog never goes away.
    monkeypatch.setattr(
        ar.AutoResponder, "_current_normalized", staticmethod(lambda tid: "trust ok")
    )

    forced = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.force_status",
        lambda tid, status: forced.append((tid, status)),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.list_terminals_by_session",
        lambda session: [{"id": "sup1", "provider": "claude_code"}],
    )
    pushed = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        lambda sender, receiver, msg: pushed.append((sender, receiver, msg)),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending",
        lambda tid, registry=None: None,
    )

    _reset_engine.on_screen("term1", FakeProvider(), ["trust ok"])

    assert len(sent) == ar.RETRY_MAX  # 1 initial + 2 retries
    assert forced == [("term1", TerminalStatus.WAITING_USER_ANSWER)]
    assert len(pushed) == 1
    assert pushed[0][1] == "sup1"
    assert "fired 3x" in pushed[0][2]


def test_retry_exhausted_respects_self_push_guard(monkeypatch, _reset_engine):
    metadata = _metadata(id="sup1", provider="claude_code")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.force_status",
        lambda tid, status: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.list_terminals_by_session",
        lambda session: [{"id": "sup1", "provider": "claude_code"}],
    )
    pushed = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        lambda sender, receiver, msg: pushed.append((sender, receiver, msg)),
    )

    _reset_engine._surface_retry_exhausted(
        "sup1",
        metadata,
        ar.Rule("r", True, "contains", "trust", ["ok"], ["Enter"]),
    )

    assert pushed == []


# ----- wait semantics --------------------------------------------------------


def test_wait_rule_surfaces_without_firing_or_pushing(monkeypatch, _reset_engine):
    sent = []
    _wire_common(monkeypatch, sent_keys=sent)
    monkeypatch.setattr(
        ar._store,
        "get_rules",
        lambda provider: [ar.Rule("r", True, "contains", "danger", [], "wait")],
    )
    pushed = []
    monkeypatch.setattr(ar.AutoResponder, "_push", lambda self, *a, **k: pushed.append(a))

    result = _reset_engine.on_screen("term1", FakeProvider(), ["danger zone"])

    assert result == TerminalStatus.WAITING_USER_ANSWER
    assert sent == []
    assert pushed == []  # wait rules don't push — it IS the rule


# ----- unknown-dialog heuristic + dedupe ------------------------------------


def test_supervisor_terminal_is_excluded_from_unknown_detection(monkeypatch, _reset_engine):
    metadata = _metadata(id="sup1", provider="claude_code")
    _wire_common(monkeypatch, metadata=metadata)
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.list_terminals_by_session",
        lambda session: [{"id": "sup1", "provider": "claude_code"}],
    )
    monkeypatch.setattr(ar._store, "get_rules", lambda provider: pytest.fail("rules checked"))
    pushed = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        lambda sender, receiver, msg: pushed.append((sender, receiver, msg)),
    )

    result = _reset_engine.on_screen(
        "sup1",
        FakeProvider(),
        ["Supervisor question", "1. Yes", "2. No", "Press enter to continue"],
    )

    assert result is None
    assert pushed == []


def test_push_refuses_to_send_to_source_terminal(monkeypatch, _reset_engine):
    metadata = _metadata(id="sup1", provider="claude_code")
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.list_terminals_by_session",
        lambda session: [{"id": "sup1", "provider": "claude_code"}],
    )
    pushed = []
    delivered = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        lambda sender, receiver, msg: pushed.append((sender, receiver, msg)),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending",
        lambda tid, registry=None: delivered.append(tid),
    )

    _reset_engine._push("sup1", metadata, "message")

    assert pushed == []
    assert delivered == []


def test_unknown_dialog_detected_and_pushed_once(monkeypatch, _reset_engine):
    _wire_common(monkeypatch)
    monkeypatch.setattr(ar._store, "get_rules", lambda provider: [])

    pushed = []
    monkeypatch.setattr(ar.AutoResponder, "_push", lambda self, tid, meta, msg: pushed.append(msg))

    screen = ["Some new prompt", "1. Yes, continue", "2. No, quit", "Press enter to continue"]
    r1 = _reset_engine.on_screen("term1", FakeProvider(), screen)
    r2 = _reset_engine.on_screen("term1", FakeProvider(), screen)  # same text, same episode

    assert r1 == TerminalStatus.WAITING_USER_ANSWER
    assert r2 == TerminalStatus.WAITING_USER_ANSWER
    assert len(pushed) == 1  # deduped — only one push per episode


def test_unknown_dialog_screen_mutations_do_not_repush_open_episode(monkeypatch, _reset_engine):
    _wire_common(monkeypatch)
    monkeypatch.setattr(ar._store, "get_rules", lambda provider: [])
    pushed = []
    monkeypatch.setattr(ar.AutoResponder, "_push", lambda self, tid, meta, msg: pushed.append(msg))

    for tick in range(5):
        _reset_engine.on_screen(
            "term1",
            FakeProvider(),
            [
                f"Prompt spinner={tick}",
                "1. Yes, continue",
                "2. No, quit",
                "Press enter to continue",
            ],
        )

    assert len(pushed) == 1


def test_unknown_episode_closes_then_respects_cross_episode_push_floor(monkeypatch, _reset_engine):
    _wire_common(monkeypatch)
    monkeypatch.setattr(ar._store, "get_rules", lambda provider: [])
    now = [1000.0]
    monkeypatch.setattr(ar.time, "monotonic", lambda: now[0])
    pushed = []
    monkeypatch.setattr(ar.AutoResponder, "_push", lambda self, tid, meta, msg: pushed.append(msg))
    screen = ["Prompt", "1. Yes, continue", "2. No, quit", "Press enter to continue"]

    assert (
        _reset_engine.on_screen("term1", FakeProvider(), screen)
        == TerminalStatus.WAITING_USER_ANSWER
    )
    assert len(pushed) == 1

    now[0] = 1001.0
    assert (
        _reset_engine.on_screen("term1", FakeProvider(), ["ordinary output"])
        == TerminalStatus.WAITING_USER_ANSWER
    )
    now[0] = 1002.0
    assert _reset_engine.on_screen("term1", FakeProvider(), ["ordinary output"]) is None

    now[0] = 1100.0
    assert (
        _reset_engine.on_screen("term1", FakeProvider(), screen)
        == TerminalStatus.WAITING_USER_ANSWER
    )
    assert len(pushed) == 1

    now[0] = 1101.0
    assert (
        _reset_engine.on_screen("term1", FakeProvider(), ["ordinary output"])
        == TerminalStatus.WAITING_USER_ANSWER
    )
    now[0] = 1102.0
    assert _reset_engine.on_screen("term1", FakeProvider(), ["ordinary output"]) is None

    now[0] = 1301.0
    assert (
        _reset_engine.on_screen("term1", FakeProvider(), screen)
        == TerminalStatus.WAITING_USER_ANSWER
    )
    assert len(pushed) == 2


def test_unknown_dialog_payload_caps_dialog_text(monkeypatch, _reset_engine):
    _wire_common(monkeypatch)
    monkeypatch.setattr(ar._store, "get_rules", lambda provider: [])
    pushed = []
    monkeypatch.setattr(ar.AutoResponder, "_push", lambda self, tid, meta, msg: pushed.append(msg))
    long_text = "x" * 2000

    _reset_engine.on_screen(
        "term1",
        FakeProvider(),
        [long_text, "1. Yes, continue", "2. No, quit", "Press enter to continue"],
    )

    dialog_text = pushed[0].split("Dialog text (normalized): ", 1)[1]
    assert len(dialog_text) <= ar.UNKNOWN_DIALOG_PAYLOAD_CHARS + 3
    assert long_text not in dialog_text


def test_ordinary_screen_is_not_flagged_as_dialog(monkeypatch, _reset_engine):
    _wire_common(monkeypatch)
    monkeypatch.setattr(ar._store, "get_rules", lambda provider: [])

    result = _reset_engine.on_screen(
        "term1", FakeProvider(), ["just some regular assistant output", "no options here"]
    )
    assert result is None


# ----- gating: kill switches -------------------------------------------------


def test_global_kill_switch_disables_engine(monkeypatch, _reset_engine):
    monkeypatch.setenv("CAO_AUTO_ANSWER", "false")
    called = []
    monkeypatch.setattr(ar.AutoResponder, "_on_screen", lambda self, *a: called.append(a))

    result = _reset_engine.on_screen("term1", FakeProvider(), ["anything"])

    assert result is None
    assert called == []


def test_provider_without_screen_detection_is_skipped(monkeypatch, _reset_engine):
    class NoScreenProvider:
        supports_screen_detection = False

    called = []
    monkeypatch.setattr(ar.AutoResponder, "_on_screen", lambda self, *a: called.append(a))

    result = _reset_engine.on_screen("term1", NoScreenProvider(), ["anything"])

    assert result is None
    assert called == []


def test_session_env_opt_out_skips_terminal(monkeypatch, _reset_engine):
    sent = []
    _wire_common(monkeypatch, sent_keys=sent, session_env={"CAO_AUTO_ANSWER": "false"})
    monkeypatch.setattr(
        ar._store,
        "get_rules",
        lambda provider: [ar.Rule("r", True, "contains", "trust", ["ok"], ["Enter"])],
    )

    result = _reset_engine.on_screen("term1", FakeProvider(), ["trust ok"])

    assert result is None
    assert sent == []


# ----- hot reload ------------------------------------------------------------


def test_rule_store_hot_reloads_on_mtime_change(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path)
    store = ar._RuleStore()
    path = tmp_path / "codex.yaml"
    path.write_text(
        "- name: r1\n  enabled: true\n  match_mode: contains\n  question: hello\n"
        "  options: []\n  answer: [Enter]\n"
    )

    rules = store.get_rules("codex")
    assert [r.name for r in rules] == ["r1"]

    # Mutate the file and force a distinct mtime (some filesystems have 1s
    # resolution) so the cache treats it as changed.
    time.sleep(0.01)
    new_mtime = path.stat().st_mtime + 1
    path.write_text(
        "- name: r2\n  enabled: true\n  match_mode: contains\n  question: world\n"
        "  options: []\n  answer: [Enter]\n"
    )
    import os

    os.utime(path, (new_mtime, new_mtime))

    rules = store.get_rules("codex")
    assert [r.name for r in rules] == ["r2"]


def test_seed_file_created_once_and_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", tmp_path)
    path = ar._rules_path("codex")
    assert path.exists()
    assert "codex-usage-resets" in path.read_text()
    assert "codex-trust-dir" in path.read_text()

    path.write_text("- name: custom\n  question: x\n  options: []\n  answer: wait\n")
    ar._rules_path("codex")  # must not overwrite
    assert "custom" in path.read_text()
    assert "codex-usage-resets" not in path.read_text()
