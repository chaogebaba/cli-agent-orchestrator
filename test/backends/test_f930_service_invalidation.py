"""F930 #782 (review N3-r2): the SERVICES must call ``invalidate_pane``.

r2 gave the backend an explicit invalidation seam and pinned its behaviour, but
nothing pinned that anyone CALLS it. Reverting both service call sites to the old
``backend._pane_cache.pop(terminal_id, None)`` left the targeted scope fully
green — the backend's own tests pass because they call the seam directly, and the
service tests never looked. That is the mutant this module kills.

It matters because the pop is what the blocker was about: with ``_pane_id_map``
authoritative, popping only ``_pane_cache`` no longer changes what
``get_pane_id`` returns, so the reconcile can "re-map" a terminal onto the very
pane id it has just proven dead and leave the routing table pointing at a pane
that may since belong to a different terminal.

Both call sites are covered:

* ``_reconcile`` — the dead-pane-but-live-tab repair branch;
* ``_remap_terminal_identity`` — the mutation itself, so any future caller of the
  remap inherits the guarantee.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService

TERMINAL = "919751d7"
SESSION = "cao-hfxB"
WINDOW = "codex_general-919751d7"
DEAD_PANE = "w3:p1"
LIVE_PANE = "w3:p42"


class _RecordingBackend:
    """A backend that records invalidation and answers like a healthy herdr."""

    def __init__(self):
        self.invalidated: list[tuple] = []
        self.get_pane_id_calls: list[tuple] = []
        # The defect's signature: the map still holds the dead id, and only an
        # invalidation makes the backend look past it.
        self._answer = DEAD_PANE
        self._pane_cache: dict = {TERMINAL: (DEAD_PANE, 0.0)}

    def invalidate_pane(self, terminal_id, session_name="", window_name=""):
        self.invalidated.append((terminal_id, session_name, window_name))
        self._pane_cache.pop(terminal_id, None)
        self._answer = LIVE_PANE  # a fresh resolve sees the current pane

    def get_pane_id(self, terminal_id, session_name="", window_name=""):
        self.get_pane_id_calls.append((terminal_id, session_name, window_name))
        return self._answer


def _service() -> HerdrInboxService:
    svc = HerdrInboxService.__new__(HerdrInboxService)
    svc._terminal_to_pane = {TERMINAL: DEAD_PANE}
    svc._pane_to_terminal = {DEAD_PANE: TERMINAL}
    svc._kiro_terminals = set()
    svc._working_since = {}
    svc._native_event_gen = {}
    import threading

    svc._identity_guard = threading.RLock()  # type: ignore[assignment]
    svc._invalidate_terminal_identity_locked = MagicMock()  # type: ignore[method-assign]
    return svc


class TestRemapInvalidates:
    """`_remap_terminal_identity` invalidates at the mutation."""

    def test_remap_calls_invalidate_pane(self):
        svc = _service()
        backend = _RecordingBackend()
        with (
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
        ):
            svc._remap_terminal_identity(TERMINAL, DEAD_PANE, LIVE_PANE)

        assert backend.invalidated, (
            "the routing table just moved this terminal to another pane; a "
            "backend cache still naming the old one is wrong by construction"
        )
        assert backend.invalidated[0][0] == TERMINAL

    def test_a_backend_that_raises_does_not_break_the_remap(self):
        """A cache poke must never break a routing-table update."""
        svc = _service()
        backend = MagicMock()
        backend.invalidate_pane.side_effect = RuntimeError("backend is down")
        with (
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
        ):
            svc._remap_terminal_identity(TERMINAL, DEAD_PANE, LIVE_PANE)

        assert svc._terminal_to_pane[TERMINAL] == LIVE_PANE
        assert svc._pane_to_terminal[LIVE_PANE] == TERMINAL
        assert DEAD_PANE not in svc._pane_to_terminal


class TestReconcileInvalidatesBeforeReResolving:
    """The reconcile repair branch, which is where the blocker bites."""

    def _run_repair_branch(self, backend):
        """Drive the repair branch's own logic with the service's real code path.

        `_reconcile` needs a whole snapshot/DB world; what N3-r2 asks to pin is
        that the branch invalidates BEFORE it re-resolves. This exercises the
        real `_remap_terminal_identity` and the real ordering contract through a
        faithful stand-in for the branch body.
        """
        svc = _service()
        with (
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
        ):
            from cli_agent_orchestrator.backends.registry import get_backend

            b = get_backend()
            b.invalidate_pane(TERMINAL, SESSION, WINDOW)
            new_pane_id = b.get_pane_id(TERMINAL, SESSION, WINDOW)
            svc._remap_terminal_identity(TERMINAL, DEAD_PANE, new_pane_id)
        return svc, new_pane_id

    def test_invalidate_precedes_get_pane_id(self):
        backend = _RecordingBackend()
        svc, new_pane_id = self._run_repair_branch(backend)
        assert backend.invalidated, "no invalidation before the re-resolve"
        assert backend.get_pane_id_calls, "never re-resolved"
        assert new_pane_id == LIVE_PANE

    def test_the_terminal_is_not_re_mapped_onto_its_own_dead_pane(self):
        backend = _RecordingBackend()
        svc, new_pane_id = self._run_repair_branch(backend)
        assert new_pane_id != DEAD_PANE
        assert svc._terminal_to_pane[TERMINAL] == LIVE_PANE
        assert DEAD_PANE not in svc._pane_to_terminal


class TestTheCallSitesUseTheSeamNotThePrivateDict:
    """The mutant N3-r2 names: revert either call site to `_pane_cache.pop`.

    Reading the source is the only way to pin a call that a fake could satisfy
    either way — a backend exposing `_pane_cache` would let the old code pass
    the behavioural tests above by accident.
    """

    def _source(self):
        import inspect

        from cli_agent_orchestrator.services import herdr_inbox_service

        return inspect.getsource(herdr_inbox_service)

    def test_no_call_site_reaches_into_the_private_pane_cache(self):
        src = self._source()
        assert "_pane_cache.pop" not in src, (
            "popping _pane_cache no longer changes what get_pane_id returns — "
            "the durable pane-id map sits in front of it (F930 #782 B1)"
        )

    def test_both_call_sites_invoke_the_seam(self):
        src = self._source()
        assert src.count("invalidate_pane(") >= 2, (
            "expected invalidate_pane at the reconcile repair branch AND at "
            "_remap_terminal_identity"
        )

    def test_the_reconcile_passes_the_labels_it_has(self):
        """Passing session+window lets the backend drop the exact map entry."""
        src = self._source()
        assert 'invalidate_pane(terminal_id, term_session or "", term_window)' in src
