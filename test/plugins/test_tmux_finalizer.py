"""Regression tests for the F330 tmux finalizer's xdist fence.

The sweep kills every tmux session whose name starts with ``cao-test-`` /
``caotest-`` on the SHARED tmux server. Each xdist worker is its own pytest
session, so both sweep sites used to fire the moment a worker ran out of work —
killing sessions its still-running siblings had just created. Measured on
grok-box-009 (2026-09-11, ``-n 4 -m "not e2e and not slow"``): gw1's teardown
killed ``cao-test-sm-ni-4823d873`` 28s before gw2 finished, and gw2's
``test_send_to_busy_worker_queues_not_injects`` got HTTP 500
``provider_launch_failed`` from ``POST /sessions``.

These tests pin the fence: a worker declines the sweep, the controller (and a
serial run, which has no ``workerinput`` either) still performs it.
"""

from __future__ import annotations

from test.plugins import tmux_finalizer
from unittest.mock import MagicMock

import pytest


def _make_config(*, is_worker: bool) -> MagicMock:
    """Minimal ``pytest.Config`` stand-in (mirrors test/plugins/test_suite_slot.py)."""
    config = MagicMock(spec=pytest.Config)
    if is_worker:
        config.workerinput = {"workerid": "gw0"}
    else:
        # Controller / serial run — xdist never set the attribute.
        del config.workerinput
    return config


class TestIsXdistWorker:
    def test_worker_config_is_a_worker(self) -> None:
        assert tmux_finalizer._is_xdist_worker(_make_config(is_worker=True)) is True

    def test_controller_config_is_not_a_worker(self) -> None:
        assert tmux_finalizer._is_xdist_worker(_make_config(is_worker=False)) is False


class TestSweepFence:
    def test_worker_never_kills(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A worker's session end is mid-run for its siblings — no kills."""
        calls: list[int] = []
        monkeypatch.setattr(tmux_finalizer, "_kill_test_sessions", lambda: calls.append(1) or 1)

        killed = tmux_finalizer.sweep_unless_xdist_worker(
            _make_config(is_worker=True), "fixture teardown"
        )

        assert killed == 0
        assert calls == [], "the sweep must not reach tmux from an xdist worker"

    def test_controller_sweeps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The controller outlives every worker, so it keeps the leak guarantee."""
        calls: list[int] = []
        monkeypatch.setattr(tmux_finalizer, "_kill_test_sessions", lambda: calls.append(1) or 3)

        killed = tmux_finalizer.sweep_unless_xdist_worker(
            _make_config(is_worker=False), "sessionfinish sweep"
        )

        assert killed == 3
        assert calls == [1]


class TestSessionFinishHook:
    def test_hook_declines_on_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []
        monkeypatch.setattr(tmux_finalizer, "_kill_test_sessions", lambda: calls.append(1) or 1)
        session = MagicMock()
        session.config = _make_config(is_worker=True)

        tmux_finalizer.pytest_sessionfinish(session, 0)

        assert calls == []

    def test_hook_sweeps_on_controller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []
        monkeypatch.setattr(tmux_finalizer, "_kill_test_sessions", lambda: calls.append(1) or 1)
        session = MagicMock()
        session.config = _make_config(is_worker=False)

        tmux_finalizer.pytest_sessionfinish(session, 0)

        assert calls == [1]
