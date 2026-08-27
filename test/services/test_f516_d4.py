"""F516 commit 4: D4 schedule_detection_retry + geometric backoff.

AC3/AC5 assert the 1/2/4(/8) backoff via a seam on the DELAY PATH (the harness
observes the requested delays), not wall-clock.
"""

from unittest.mock import MagicMock

from cli_agent_orchestrator.services import status_monitor as sm


class _FakeLoop:
    def __init__(self):
        self.delays = []

    def call_soon_threadsafe(self, fn):
        fn()

    def call_later(self, delay, callback, *args):
        self.delays.append(delay)
        return MagicMock()


def _monitor_with_loop(monkeypatch, loop, provider=object()):
    monitor = sm.StatusMonitor()
    monitor._loop = loop
    monkeypatch.setattr(sm.provider_manager, "get_provider", lambda _tid: provider)
    return monitor


def test_backoff_is_geometric_1_2_4_8_capped(monkeypatch):
    loop = _FakeLoop()
    monitor = _monitor_with_loop(monkeypatch, loop)
    for _ in range(8):
        monitor.schedule_detection_retry("term1")
    assert loop.delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_chunk_reset_edge_snaps_backoff_to_zero(monkeypatch):
    loop = _FakeLoop()
    monitor = _monitor_with_loop(monkeypatch, loop)
    monitor.schedule_detection_retry("term1")
    monitor.schedule_detection_retry("term1")
    monkeypatch.setattr(monitor, "_detect_screen_with_trust", lambda *_a: (None, False, None))
    monkeypatch.setattr(monitor, "_apply_detection", lambda *a, **k: None)
    monitor._schedule_screen_detection("term1", object())
    loop.delays.clear()
    monitor.schedule_detection_retry("term1")
    assert loop.delays == [1.0]


def test_no_loop_is_a_noop_never_immediate_detect(monkeypatch):
    monitor = sm.StatusMonitor()
    monitor._loop = None
    called = {"n": 0}

    def _get(_tid):
        called["n"] += 1
        return object()

    monkeypatch.setattr(sm.provider_manager, "get_provider", _get)
    monkeypatch.setattr(sm.StatusMonitor, "_running_loop", staticmethod(lambda: None))
    monitor.schedule_detection_retry("term1")
    assert called["n"] == 0


def test_provider_gone_is_a_noop(monkeypatch):
    loop = _FakeLoop()
    monitor = _monitor_with_loop(monkeypatch, loop, provider=None)
    monitor.schedule_detection_retry("term1")
    assert loop.delays == []


def test_explicit_delay_bypasses_backoff_counter(monkeypatch):
    loop = _FakeLoop()
    monitor = _monitor_with_loop(monkeypatch, loop)
    monitor.schedule_detection_retry("term1", delay_s=0.5)
    monitor.schedule_detection_retry("term1", delay_s=0.5)
    assert loop.delays == [0.5, 0.5]


def test_clear_terminal_resets_backoff(monkeypatch):
    loop = _FakeLoop()
    monitor = _monitor_with_loop(monkeypatch, loop)
    monitor.schedule_detection_retry("term1")
    monitor.schedule_detection_retry("term1")
    monitor.clear_terminal("term1")
    loop.delays.clear()
    monitor.schedule_detection_retry("term1")
    assert loop.delays == [1.0]
