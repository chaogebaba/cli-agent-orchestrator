"""Tests for composer draft preservation before input injection."""

from cli_agent_orchestrator.services import draft_guard


class FakeProvider:
    supports_draft_preservation = True
    composer_clear_keys = ["C-a", "C-k"]
    paste_submit_delay = 0.1

    def read_composer_draft(self, screen_lines):
        # Mirror provider contract: "" = visible empty composer; None = unknown.
        # Tests feed "\\n" captures (splitlines → []) for empty.
        if not screen_lines:
            return ""
        return screen_lines[0]


def _fast_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_guard, "DRAFT_LOG_DIR", tmp_path)
    monkeypatch.setattr(draft_guard, "DRAFT_STABILITY_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(draft_guard, "DRAFT_STABILITY_RECHECK_SECONDS", 0)
    monkeypatch.setattr(draft_guard, "DRAFT_STABILITY_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(draft_guard, "DRAFT_CLEAR_RECHECK_DELAY_SECONDS", 0)
    monkeypatch.setattr(draft_guard, "DRAFT_CLEAR_PROBE_RECHECK_DELAY_SECONDS", 0)
    monkeypatch.setattr(draft_guard, "DRAFT_CLEAR_PROBE_NONE_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(draft_guard.time, "sleep", lambda _: None)


def test_preserve_logs_clears_and_restores(monkeypatch, tmp_path):
    _fast_guard(monkeypatch, tmp_path)
    metadata = {"tmux_session": "cao-test", "tmux_window": "win"}
    # Escape-preserving capture is preferred; sequence:
    # read, recheck stable, clear-probe re-read (changed → empty), clear confirm empty.
    captures = iter(["draft\n", "draft\n", "\n", "\n"])
    backend = type("Backend", (), {})()
    backend.send_special_key_calls = []
    backend.send_keys_calls = []

    def send_special_key(*args):
        backend.send_special_key_calls.append(args)

    def send_keys(*args, **kwargs):
        backend.send_keys_calls.append((args, kwargs))

    backend.send_special_key = send_special_key
    backend.send_keys = send_keys
    backend.get_history = lambda *args, **kwargs: next(captures)
    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: None)
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
    captures = iter(["a\n", "ab\n", "abc\n", "abc\n", "\n", "\n"])
    backend = type("Backend", (), {})()
    backend.send_special_key = lambda *args: None
    backend.get_history = lambda *args, **kwargs: next(captures)

    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: None)
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)

    preserved = draft_guard.preserve_draft_before_send("term1", metadata, FakeProvider())

    assert preserved is not None
    assert preserved.text == "abc"
    assert "abc" in (tmp_path / "term1.log").read_text()


def test_clear_immune_text_returns_none_as_ghost(monkeypatch, tmp_path):
    _fast_guard(monkeypatch, tmp_path)
    metadata = {"tmux_session": "cao-test", "tmux_window": "win"}
    backend = type("Backend", (), {})()
    calls = []
    backend.send_special_key = lambda *args: calls.append(args)
    backend.get_history = lambda *args, **kwargs: "ghost\n"

    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: None)
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)

    preserved = draft_guard.preserve_draft_before_send("term1", metadata, FakeProvider())

    assert preserved is None
    assert calls == [
        ("cao-test", "win", "C-a"),
        ("cao-test", "win", "C-k"),
    ]
    assert not (tmp_path / "term1.log").exists()


def test_clear_probe_capture_failure_preserves_as_real_draft(monkeypatch, tmp_path):
    """After clear keys, None re-reads (exhausted retries) ⇒ conservative real draft."""
    _fast_guard(monkeypatch, tmp_path)
    metadata = {"tmux_session": "cao-test", "tmux_window": "win"}
    # Initial reads succeed; clear-probe re-reads raise → None after retries;
    # then clear-loop capture reports empty composer.
    phase = {"n": 0}
    probe_attempts = 1 + draft_guard.DRAFT_CLEAR_PROBE_NONE_RETRIES

    def get_history(*args, **kwargs):
        phase["n"] += 1
        if phase["n"] <= 2:
            return "draft\n"
        if phase["n"] <= 2 + probe_attempts:
            raise RuntimeError("gone")
        return "\n"

    backend = type("Backend", (), {})()
    backend.send_special_key = lambda *args: None
    backend.get_history = get_history
    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: None)
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)

    preserved = draft_guard.preserve_draft_before_send("term1", metadata, FakeProvider())

    assert preserved is not None
    assert preserved.text == "draft"
    # 2 successful + probe None retries + at least one clear-loop empty read
    assert phase["n"] >= 2 + probe_attempts + 1


def test_clear_probe_none_retry_then_ghost(monkeypatch, tmp_path):
    """None re-read is retried; a later unchanged capture classifies as ghost."""
    _fast_guard(monkeypatch, tmp_path)
    metadata = {"tmux_session": "cao-test", "tmux_window": "win"}
    results = iter(
        [
            "ghost\n",  # initial
            "ghost\n",  # stable
            RuntimeError("glitch"),  # probe attempt 1 → None
            RuntimeError("glitch"),  # probe attempt 2 → None
            "ghost\n",  # probe attempt 3 → unchanged ⇒ ghost
        ]
    )

    def get_history(*args, **kwargs):
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    backend = type("Backend", (), {})()
    calls = []
    backend.send_special_key = lambda *args: calls.append(args)
    backend.get_history = get_history
    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: None)
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)

    preserved = draft_guard.preserve_draft_before_send("term1", metadata, FakeProvider())

    assert preserved is None
    assert calls == [
        ("cao-test", "win", "C-a"),
        ("cao-test", "win", "C-k"),
    ]
    assert not (tmp_path / "term1.log").exists()


def test_clear_loop_cap_never_restores_when_exhausted(monkeypatch, tmp_path):
    _fast_guard(monkeypatch, tmp_path)
    monkeypatch.setattr(draft_guard, "DRAFT_CLEAR_MAX_ITERATIONS", 2)
    metadata = {"tmux_session": "cao-test", "tmux_window": "win"}
    backend = type("Backend", (), {})()
    calls = []
    backend.send_special_key = lambda *args: calls.append(args)
    # Real draft that becomes stuck partial after clear-probe sees a change.
    captures = iter(
        [
            "draft\n",  # initial
            "draft\n",  # stable
            "partial\n",  # clear-probe: changed ⇒ real
            "partial\n",  # clear loop 1
            "partial\n",  # clear loop 2
            "partial\n",  # clear loop check after last keys
        ]
    )
    backend.get_history = lambda *args, **kwargs: next(captures)

    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: None)
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)

    preserved = draft_guard.preserve_draft_before_send("term1", metadata, FakeProvider())

    assert preserved is None
    assert calls == [
        ("cao-test", "win", "C-a"),
        ("cao-test", "win", "C-k"),
        ("cao-test", "win", "C-a"),
        ("cao-test", "win", "C-k"),
        ("cao-test", "win", "C-a"),
        ("cao-test", "win", "C-k"),
    ]


def test_falls_back_to_capture_pane_when_rendered_screen_missing(monkeypatch, tmp_path):
    _fast_guard(monkeypatch, tmp_path)
    metadata = {"tmux_session": "cao-test", "tmux_window": "win"}
    backend = type("Backend", (), {})()
    captures = iter(["draft\n", "draft\n", "\n", "\n"])
    backend.get_history = lambda *args, **kwargs: next(captures)
    backend.send_special_key = lambda *args: None

    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: None)
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)

    preserved = draft_guard.preserve_draft_before_send("term1", metadata, FakeProvider())

    assert preserved is not None
    assert preserved.text == "draft"


def test_plain_provider_gets_strip_escapes_true(monkeypatch, tmp_path):
    """Grok-style providers (no escape opt-in) always receive plain capture."""
    _fast_guard(monkeypatch, tmp_path)
    metadata = {"tmux_session": "cao-test", "tmux_window": "win"}
    kwargs_seen = []
    backend = type("Backend", (), {})()
    captures = iter(["draft\n", "draft\n", "\n", "\n"])

    def get_history(*args, **kwargs):
        kwargs_seen.append(kwargs)
        return next(captures)

    backend.get_history = get_history
    backend.send_special_key = lambda *args: None
    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: None)
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)

    # FakeProvider has no composer_parse_accepts_escapes (defaults False).
    draft_guard.preserve_draft_before_send("term1", metadata, FakeProvider())

    assert kwargs_seen
    assert all(k.get("strip_escapes") is True for k in kwargs_seen)


class EscapeAwareFakeProvider(FakeProvider):
    """Codex-like: can parse escape-preserving capture for dim-ghost detection."""

    composer_parse_accepts_escapes = True


def test_escape_aware_provider_gets_strip_escapes_false(monkeypatch, tmp_path):
    """Opt-in providers (codex) request escape-preserving capture first."""
    _fast_guard(monkeypatch, tmp_path)
    metadata = {"tmux_session": "cao-test", "tmux_window": "win"}
    kwargs_seen = []
    backend = type("Backend", (), {})()
    captures = iter(["draft\n", "draft\n", "\n", "\n"])

    def get_history(*args, **kwargs):
        kwargs_seen.append(kwargs)
        return next(captures)

    backend.get_history = get_history
    backend.send_special_key = lambda *args: None
    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _: None)
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)

    draft_guard.preserve_draft_before_send("term1", metadata, EscapeAwareFakeProvider())

    assert kwargs_seen
    # Primary + clear-probe reads should prefer -e (strip_escapes=False).
    assert any(k.get("strip_escapes") is False for k in kwargs_seen)
    # Never only-plain when opt-in path succeeds on first escape capture.
    assert kwargs_seen[0].get("strip_escapes") is False
