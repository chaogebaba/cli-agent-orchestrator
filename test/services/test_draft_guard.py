"""Tests for composer draft preservation before input injection."""

from cli_agent_orchestrator.services import draft_guard


class FakeProvider:
    supports_draft_preservation = True
    composer_clear_keys = ["C-a", "C-k"]
    paste_submit_delay = 0.1

    def read_composer_draft(self, screen_lines):
        return screen_lines[0]


def _fast_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_guard, "DRAFT_LOG_DIR", tmp_path)
    monkeypatch.setattr(draft_guard, "DRAFT_STABILITY_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(draft_guard, "DRAFT_STABILITY_RECHECK_SECONDS", 0)
    monkeypatch.setattr(draft_guard, "DRAFT_STABILITY_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(draft_guard, "DRAFT_CLEAR_RECHECK_DELAY_SECONDS", 0)
    monkeypatch.setattr(draft_guard.time, "sleep", lambda _: None)


def test_preserve_logs_clears_and_restores(monkeypatch, tmp_path):
    _fast_guard(monkeypatch, tmp_path)
    metadata = {"tmux_session": "cao-test", "tmux_window": "win"}
    screens = iter([["draft"], ["draft"], ["draft"], [""]])
    backend = type("Backend", (), {})()
    backend.send_special_key_calls = []
    backend.send_keys_calls = []

    def send_special_key(*args):
        backend.send_special_key_calls.append(args)

    def send_keys(*args, **kwargs):
        backend.send_keys_calls.append((args, kwargs))

    backend.send_special_key = send_special_key
    backend.send_keys = send_keys
    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: next(screens))
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)

    preserved = draft_guard.preserve_draft_before_send("term1", metadata, FakeProvider())

    assert preserved is not None
    assert preserved.text == "draft"
    assert backend.send_special_key_calls == [
        ("cao-test", "win", "C-a"),
        ("cao-test", "win", "C-k"),
    ]
    log_text = (tmp_path / "term1.log").read_text()
    assert "terminal_id=term1" in log_text
    assert "draft" in log_text

    preserved.restore(backend)

    assert backend.send_keys_calls == [
        (
            ("cao-test", "win", "draft"),
            {
                "enter_count": 0,
                "force_bracketed_paste": True,
                "submit_delay": 0.1,
            },
        )
    ]


def test_stability_gate_waits_for_two_matching_reads(monkeypatch, tmp_path):
    _fast_guard(monkeypatch, tmp_path)
    metadata = {"tmux_session": "cao-test", "tmux_window": "win"}
    screens = iter([["a"], ["ab"], ["abc"], ["abc"], ["abc"], [""]])
    backend = type("Backend", (), {})()
    backend.send_special_key = lambda *args: None

    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: next(screens))
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)

    preserved = draft_guard.preserve_draft_before_send("term1", metadata, FakeProvider())

    assert preserved is not None
    assert preserved.text == "abc"
    assert "abc" in (tmp_path / "term1.log").read_text()


def test_clear_loop_cap_degrades_to_delivery(monkeypatch, tmp_path):
    _fast_guard(monkeypatch, tmp_path)
    monkeypatch.setattr(draft_guard, "DRAFT_CLEAR_MAX_ITERATIONS", 2)
    metadata = {"tmux_session": "cao-test", "tmux_window": "win"}
    backend = type("Backend", (), {})()
    calls = []
    backend.send_special_key = lambda *args: calls.append(args)

    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: ["draft"])
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)

    preserved = draft_guard.preserve_draft_before_send("term1", metadata, FakeProvider())

    assert preserved is not None
    assert preserved.text == "draft"
    assert calls == [
        ("cao-test", "win", "C-a"),
        ("cao-test", "win", "C-k"),
        ("cao-test", "win", "C-a"),
        ("cao-test", "win", "C-k"),
    ]


def test_falls_back_to_capture_pane_when_rendered_screen_missing(monkeypatch, tmp_path):
    _fast_guard(monkeypatch, tmp_path)
    metadata = {"tmux_session": "cao-test", "tmux_window": "win"}
    backend = type("Backend", (), {})()
    captures = iter(["draft\n", "draft\n", "draft\n", "\n"])
    backend.get_history = lambda *args, **kwargs: next(captures)
    backend.send_special_key = lambda *args: None

    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: None)
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)

    preserved = draft_guard.preserve_draft_before_send("term1", metadata, FakeProvider())

    assert preserved is not None
    assert preserved.text == "draft"
