"""F597 #454 pt2: auto-responder DELIVERY fixes — settle + re-arm.

Root cause (incident 55e84b8a): codex-trust-dir matched and FIRED 22x in ~28s yet
the dialog never cleared — the Enters landed before codex's TUI input handler was
armed — then the terminal latched retry-exhausted and no further tick was ever
scheduled on the silent pane, so it stayed stuck until a human pressed Enter.

(a) SETTLE: before the FIRST send of an episode, require the matched frame to be
    byte-stable across two captures SETTLE_INTERVAL_S apart, so keys are never
    sent into an unarmed handler.
(c) RE-ARM: retry-exhaustion must not latch off forever — while the same dialog
    signature persists, re-fire on a bounded backoff (5/15/45s, then 60s, cap
    10min), and stop once the dialog clears.

Fake backend records (monotonic_ts, key) for every send so the tests can assert
WHEN the first send happens and that re-arm fires after exhaustion.
"""

from __future__ import annotations

from typing import Any

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderCapabilities
from cli_agent_orchestrator.services import auto_responder as ar

TRUST_SCREEN = [
    "Do you trust the contents of this directory?",
    "› 1. Yes, continue",
    "  2. No, quit",
    "Press enter to continue",
]
TRUST_RULE = ar.Rule(
    "codex-trust-dir",
    True,
    "contains",
    "Do you trust the contents of this directory?",
    ["Yes, continue", "No, quit"],
    ["Enter"],
)


class _WaitingProvider:
    """Classifier says WAITING so the D2 fast-path makes the match eligible on
    the first eval (isolates the settle/re-arm behaviour under test)."""

    capabilities = ProviderCapabilities(supports_screen_detection=True)

    def get_status_from_screen(self, _lines):
        return TerminalStatus.WAITING_USER_ANSWER


class _VirtualClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture()
def clock(monkeypatch):
    c = _VirtualClock()
    monkeypatch.setattr(ar, "_clock", c)
    # settle sleep advances the virtual clock instead of real waiting.
    monkeypatch.setattr(ar, "_clock_sleep", lambda s: c.advance(s))
    # time.monotonic is used by cooldown; align it to the virtual clock.
    monkeypatch.setattr(ar.time, "monotonic", c)
    monkeypatch.setattr(ar.time, "sleep", lambda _s: None)
    return c


@pytest.fixture()
def sends(clock, monkeypatch):
    recorded: list[tuple[float, str]] = []

    class FakeBackend:
        def send_special_key(self, _session, _window, key):
            recorded.append((clock.t, key))

        def get_native_status(self, _s, _w):
            return None

        def supports_event_inbox(self):
            return False

    monkeypatch.setattr(
        "cli_agent_orchestrator.backends.registry.get_backend", lambda: FakeBackend()
    )
    return recorded


def _wire(monkeypatch, *, screen, rendered=None):
    metadata = {
        "id": "term1",
        "tmux_session": "cao-sess",
        "tmux_window": "win",
        "provider": "codex",
        "lifecycle_generation": 0,
    }
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata", lambda tid: metadata
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_env.get_session_env", lambda _s: {}
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.list_terminals_by_session", lambda _s: []
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.seam_activation.receiver_state_active", lambda _op: False
    )
    monkeypatch.setattr(ar._store, "get_rules", lambda _p: [TRUST_RULE])
    # _effect_barrier re-captures via _capture_fresh; return the matched frame.
    monkeypatch.setattr(
        ar.AutoResponder,
        "_capture_fresh",
        staticmethod(
            lambda _metadata, lines, terminal_id=None, provider=None: (
                ar.normalize_screen(list(rendered if rendered is not None else screen)),
                list(rendered if rendered is not None else screen),
            )
        ),
    )
    # Skip the delivery-lock/DB incarnation fence; run the effect directly.
    monkeypatch.setattr(
        ar.AutoResponder,
        "_run_fenced_effect",
        lambda self, tid, inc, effect: (effect() or True),
    )
    monkeypatch.setattr(
        ar.AutoResponder,
        "_incarnation_matches_under_delivery_lock",
        lambda self, tid, expected: True,
    )
    monkeypatch.setattr(ar.AutoResponder, "_incarnation_is_current", lambda self, tid, inc: True)
    # run the verify/retry thread synchronously
    import threading as _t

    class _SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._t, self._a, self._k = target, args, kwargs or {}

        def start(self):
            self._t(*self._a, **self._k)

    monkeypatch.setattr(ar.threading, "Thread", _SyncThread)
    return metadata


# ---------------------------------------------------------------------------
# (a) SETTLE
# ---------------------------------------------------------------------------


def test_settle_delays_first_send_until_frame_stable(clock, sends, monkeypatch):
    """The first send happens only AFTER the two-capture settle window, i.e. at
    t >= SETTLE_INTERVAL_S — never at t=0 off the very first eval."""
    _wire(monkeypatch, screen=TRUST_SCREEN)
    engine = ar.AutoResponder()
    t0 = clock.t
    # settle captures return the same matched frame (stable) both times.
    monkeypatch.setattr(
        engine, "_settle_capture", lambda tid, chrome=None: ar.dialog_region(TRUST_SCREEN)
    )
    engine.on_screen("term1", _WaitingProvider(), TRUST_SCREEN)

    assert sends, "expected the settle to pass and an Enter to be sent"
    first_ts = sends[0][0]
    assert first_ts - t0 >= ar.SETTLE_INTERVAL_S, (
        f"first send at +{first_ts - t0}s must be at or after the settle interval "
        f"{ar.SETTLE_INTERVAL_S}s, not t=0"
    )


def test_settle_unstable_frame_withholds_send(clock, sends, monkeypatch):
    """If the frame keeps changing across the two settle captures, NO key is
    sent this tick (the handler is not yet armed)."""
    _wire(monkeypatch, screen=TRUST_SCREEN)
    engine = ar.AutoResponder()
    frames = iter(
        [ar.dialog_region(TRUST_SCREEN), ar.dialog_region(TRUST_SCREEN + ["still painting…"])]
    )
    monkeypatch.setattr(engine, "_settle_capture", lambda tid, chrome=None: next(frames))
    engine.on_screen("term1", _WaitingProvider(), TRUST_SCREEN)
    assert sends == [], "unstable settle must withhold the first send"


def test_mutant_remove_settle_sends_at_t0(clock, sends, monkeypatch):
    """MUTANT: neutralize the settle gate (always-pass, no capture/sleep) → the
    first send happens at t=0. Proves settle is what introduces the delay."""
    _wire(monkeypatch, screen=TRUST_SCREEN)
    engine = ar.AutoResponder()
    t0 = clock.t
    monkeypatch.setattr(engine, "_settle_before_first_send", lambda *a, **k: True)
    engine.on_screen("term1", _WaitingProvider(), TRUST_SCREEN)
    assert sends, "mutant should still fire"
    assert sends[0][0] == t0, "without settle the first send is at t=0 (no delay)"


# ---------------------------------------------------------------------------
# (c) RE-ARM
# ---------------------------------------------------------------------------


def _exhaust_into_latch(engine, monkeypatch):
    """Drive the engine to retry-exhaustion: settle passes, every retry sees the
    dialog still up, so _verify_and_retry surfaces retry-exhausted and seeds the
    re-arm state."""
    monkeypatch.setattr(
        engine, "_settle_capture", lambda tid, chrome=None: ar.dialog_region(TRUST_SCREEN)
    )
    # retry loop always sees the dialog still present (never clears)
    monkeypatch.setattr(
        ar.AutoResponder,
        "_current_normalized",
        staticmethod(lambda tid: ar.dialog_region(TRUST_SCREEN)),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.force_status",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(engine, "_push", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_request_detection_retry", lambda *a, **k: None)


def test_rearm_fires_after_exhaustion_when_frame_persists(clock, sends, monkeypatch):
    _wire(monkeypatch, screen=TRUST_SCREEN)
    engine = ar.AutoResponder()
    _exhaust_into_latch(engine, monkeypatch)

    engine.on_screen("term1", _WaitingProvider(), TRUST_SCREEN)
    # It fired (initial + up to RETRY_MAX) and is now latched retry-exhausted.
    assert "term1" in engine._retry_exhausted
    assert "term1" in engine._rearm_state
    n_before = len(sends)

    # Not yet due (backoff[0] = 5s): a re-eval HOLDs, no new send.
    clock.advance(1.0)
    engine.on_screen("term1", _WaitingProvider(), TRUST_SCREEN)
    assert len(sends) == n_before, "re-arm must not fire before the first backoff elapses"

    # Past the first backoff (5s): re-arm fires a recovery Enter.
    clock.advance(ar.REARM_BACKOFF_S[0])
    engine.on_screen("term1", _WaitingProvider(), TRUST_SCREEN)
    assert len(sends) > n_before, "re-arm must re-fire once the backoff elapses"


def test_rearm_stops_when_dialog_clears(clock, sends, monkeypatch):
    _wire(monkeypatch, screen=TRUST_SCREEN)
    engine = ar.AutoResponder()
    _exhaust_into_latch(engine, monkeypatch)
    engine.on_screen("term1", _WaitingProvider(), TRUST_SCREEN)
    assert "term1" in engine._retry_exhausted

    # The dialog clears: a screen with no matching rule ends the episode and
    # drops the latch + re-arm state.
    cleared = ["• working on it…", "no dialog here"]
    engine.on_screen("term1", _WaitingProvider(), cleared)
    assert "term1" not in engine._retry_exhausted
    assert "term1" not in engine._rearm_state

    # A later eval, even past the backoff, does NOT re-fire (nothing latched).
    n = len(sends)
    clock.advance(ar.REARM_BACKOFF_S[0] + 1.0)
    engine.on_screen("term1", _WaitingProvider(), cleared)
    assert len(sends) == n


def test_mutant_remove_rearm_no_send_after_exhaustion(clock, sends, monkeypatch):
    """MUTANT: neutralize the re-arm gate (always 'hold') → once latched, NO send
    ever happens again, reproducing the latch-off-forever bug."""
    _wire(monkeypatch, screen=TRUST_SCREEN)
    engine = ar.AutoResponder()
    _exhaust_into_latch(engine, monkeypatch)
    engine.on_screen("term1", _WaitingProvider(), TRUST_SCREEN)
    n = len(sends)

    monkeypatch.setattr(engine, "_rearm_gate", lambda *a, **k: "hold")
    for _ in range(5):
        clock.advance(ar.REARM_BACKOFF_S[0] + 1.0)
        engine.on_screen("term1", _WaitingProvider(), TRUST_SCREEN)
    assert len(sends) == n, "with re-arm disabled the latched terminal never re-fires"
