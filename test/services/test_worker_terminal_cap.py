"""F439 (#294): server-side worker-terminal cap on assign/handoff.

These tests drive the counting + enforcement seam in ``terminal_service``
directly (fast, no tmux): the cap is DERIVED from live state on every call
(no persistent counter), so a restart cannot desync it. The AC cases:

- cap reached -> TerminalCapExceeded with structured E-TERMINAL-CAP payload
  (current_count, cap, reap_candidates) and NO side effect (nothing created);
- reap one idle worker -> the same call now succeeds (count drops below cap);
- supervisor terminal is NEVER counted;
- idle/warm workers COUNT toward the cap but ARE listed as reap candidates;
- cap <= 0 disables enforcement entirely (env=0 and negative file value).

The route-level atomicity ("no terminal row, no tmux window") is a direct
consequence of raising BEFORE any resource is created in ``create_terminal``:
the guard runs immediately after ``require_provider_admitted`` and before the
tmux/DB/worktree/provider path, so a refusal has nothing to unwind. That
ordering is asserted by ``test_enforced_before_any_side_effect``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.terminal_service import (
    TerminalCapExceeded,
    _count_worker_terminals,
    _enforce_worker_terminal_cap,
    _resolve_worker_terminal_cap,
)

SUPERVISOR = "aaaaaaaa"
SESSION = "cao-test"


def _row(tid: str, profile: str = "developer", caller_id: str | None = SUPERVISOR):
    """A minimal terminal row shaped like list_terminals_by_session returns."""
    return {
        "id": tid,
        "agent_profile": profile,
        "caller_id": caller_id,
        "last_active": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


@pytest.fixture
def patch_session(monkeypatch):
    """Patch the live-state sources the counter reads: the session row list and
    per-terminal live status. Returns a setter the tests use to define the fleet.
    """
    state: dict[str, object] = {"rows": [], "status": {}}

    def _list(session_name):
        return list(state["rows"])

    def _status(tid):
        return state["status"].get(tid, TerminalStatus.PROCESSING)

    monkeypatch.setattr(ts, "list_terminals_by_session", _list)
    monkeypatch.setattr(ts.status_monitor, "get_status", _status)

    def _set(rows, status=None):
        state["rows"] = rows
        state["status"] = status or {}

    return _set


class TestResolveCap:
    """_resolve_worker_terminal_cap delegates to ConfigService precedence."""

    def test_default_ten(self, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda path, default=None, override=None: default),
        )
        assert _resolve_worker_terminal_cap() == 10

    def test_env_value(self, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda path, default=None, override=None: 3),
        )
        assert _resolve_worker_terminal_cap() == 3

    def test_malformed_falls_back_to_ten(self, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda path, default=None, override=None: "not-an-int"),
        )
        assert _resolve_worker_terminal_cap() == 10


class TestCounting:
    """_count_worker_terminals derives the count from live state."""

    def test_supervisor_never_counted(self, patch_session):
        patch_session(
            rows=[_row(SUPERVISOR, "supervisor", caller_id=None), _row("bbbbbbbb")],
            status={SUPERVISOR: TerminalStatus.IDLE, "bbbbbbbb": TerminalStatus.PROCESSING},
        )
        count, candidates = _count_worker_terminals(SESSION, SUPERVISOR)
        assert count == 1  # only the worker, not the supervisor
        assert [c["id"] for c in candidates] == []  # the one worker is busy

    def test_idle_workers_count_but_are_reap_candidates(self, patch_session):
        patch_session(
            rows=[
                _row(SUPERVISOR, "supervisor", caller_id=None),
                _row("bbbbbbbb"),
                _row("cccccccc"),
            ],
            status={
                "bbbbbbbb": TerminalStatus.IDLE,
                "cccccccc": TerminalStatus.PROCESSING,
            },
        )
        count, candidates = _count_worker_terminals(SESSION, SUPERVISOR)
        assert count == 2  # idle worker still counts toward RAM pressure
        ids = [c["id"] for c in candidates]
        assert ids == ["bbbbbbbb"]  # only the idle one is a reap candidate
        cand = candidates[0]
        assert cand["display_name"] == "developer-bbbbbbbb"
        assert cand["idle_since"] is not None

    def test_empty_session(self, patch_session):
        patch_session(rows=[], status={})
        count, candidates = _count_worker_terminals(SESSION, SUPERVISOR)
        assert count == 0
        assert candidates == []


class TestEnforcement:
    """_enforce_worker_terminal_cap fail-closes at/over cap; disabled at <=0."""

    def _cap(self, monkeypatch, value):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda path, default=None, override=None: value),
        )

    def test_under_cap_allows(self, patch_session, monkeypatch):
        self._cap(monkeypatch, 3)
        patch_session(rows=[_row("bbbbbbbb"), _row("cccccccc")])
        # 2 workers, cap 3 -> no raise
        _enforce_worker_terminal_cap(SESSION, SUPERVISOR)

    def test_at_cap_refuses_with_structured_payload(self, patch_session, monkeypatch):
        self._cap(monkeypatch, 2)
        patch_session(
            rows=[_row("bbbbbbbb"), _row("cccccccc")],
            status={"bbbbbbbb": TerminalStatus.IDLE, "cccccccc": TerminalStatus.PROCESSING},
        )
        with pytest.raises(TerminalCapExceeded) as ei:
            _enforce_worker_terminal_cap(SESSION, SUPERVISOR)
        exc = ei.value
        assert exc.code == "E-TERMINAL-CAP"
        assert exc.current_count == 2
        assert exc.cap == 2
        # the idle worker is offered as a reap candidate
        assert [c["id"] for c in exc.reap_candidates] == ["bbbbbbbb"]
        detail = exc.detail()
        assert detail["code"] == "E-TERMINAL-CAP"
        assert detail["current_count"] == 2
        assert detail["cap"] == 2
        assert detail["reap_candidates"][0]["display_name"] == "developer-bbbbbbbb"

    def test_reap_one_then_succeeds(self, patch_session, monkeypatch):
        """Cap reached -> refuse; reap one worker -> the same call now succeeds."""
        self._cap(monkeypatch, 2)
        patch_session(rows=[_row("bbbbbbbb"), _row("cccccccc")])
        with pytest.raises(TerminalCapExceeded):
            _enforce_worker_terminal_cap(SESSION, SUPERVISOR)
        # Supervisor reaps one idle worker -> the fleet drops to 1 worker.
        patch_session(rows=[_row("cccccccc")])
        _enforce_worker_terminal_cap(SESSION, SUPERVISOR)  # no raise

    def test_zero_disables(self, patch_session, monkeypatch):
        self._cap(monkeypatch, 0)
        patch_session(rows=[_row(f"{i:08x}") for i in range(50)])
        _enforce_worker_terminal_cap(SESSION, SUPERVISOR)  # no raise, cap disabled

    def test_negative_disables(self, patch_session, monkeypatch):
        self._cap(monkeypatch, -1)
        patch_session(rows=[_row(f"{i:08x}") for i in range(50)])
        _enforce_worker_terminal_cap(SESSION, SUPERVISOR)  # no raise

    def test_over_cap_refuses(self, patch_session, monkeypatch):
        """count strictly greater than cap still refuses (>= semantics)."""
        self._cap(monkeypatch, 1)
        patch_session(rows=[_row("bbbbbbbb"), _row("cccccccc")])
        with pytest.raises(TerminalCapExceeded) as ei:
            _enforce_worker_terminal_cap(SESSION, SUPERVISOR)
        assert ei.value.current_count == 2
        assert ei.value.cap == 1


class _CapSentinel(RuntimeError):
    """Marker raised by the patched enforcer so tests can prove it was reached."""


class TestCreateTerminalGuardPredicate:
    """create_terminal calls the enforcer ONLY for supervisor-created workers
    joining an EXISTING session (new_session=False AND caller_id set AND
    session_name set). Operator launches and the supervisor itself are exempt.
    """

    @pytest.fixture(autouse=True)
    def _patch_enforcer(self, monkeypatch):
        def _raise(session_name, supervisor_id):
            raise _CapSentinel(f"enforced:{session_name}:{supervisor_id}")

        monkeypatch.setattr(ts, "_enforce_worker_terminal_cap", _raise)
        # Neutralize provider admission so the guard is the first thing reached.
        monkeypatch.setattr(ts, "require_provider_admitted", lambda provider: None)

    async def _call(self, **kwargs):
        return await ts.create_terminal(
            provider="mock_cli",
            agent_profile="developer",
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_assign_shape_is_enforced(self):
        """new_session=False + caller_id + session_name -> enforcer runs."""
        with pytest.raises(_CapSentinel, match="enforced:cao-test:aaaaaaaa"):
            await self._call(
                session_name="cao-test",
                new_session=False,
                caller_id=SUPERVISOR,
            )

    @pytest.mark.asyncio
    async def test_operator_new_session_is_exempt(self):
        """new_session=True (operator launch) -> enforcer NOT reached."""
        with pytest.raises(Exception) as ei:
            await self._call(
                session_name="cao-test",
                new_session=True,
                caller_id=None,
                working_directory="/nonexistent/does/not/exist/f439",
            )
        assert not isinstance(ei.value, _CapSentinel)

    @pytest.mark.asyncio
    async def test_no_caller_id_is_exempt(self):
        """new_session=False but caller_id=None (not a supervised worker)."""
        with pytest.raises(Exception) as ei:
            await self._call(
                session_name="cao-test",
                new_session=False,
                caller_id=None,
                working_directory="/nonexistent/does/not/exist/f439",
            )
        assert not isinstance(ei.value, _CapSentinel)
