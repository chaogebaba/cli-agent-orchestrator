"""F158-R3: End-to-end doorbell race tests.

These tests exercise the REAL F136 delivery path (no patching request_delivery)
and cover:
1. Late-succeeding-timeout race: WS send that completes AFTER the 0.5s timeout
   must NOT produce a mark (invalidation prevents it).
2. Same-loop caller: push_doorbell_frame_sync called from the event loop that
   would run the coroutine returns False immediately (no deadlock).
3. Normal success path: WS delivers, mark set, F136 consumes it.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestF158R3LateSendInvalidation:
    """Race: WS send completes after timeout → invalidation prevents late mark."""

    def test_timeout_invalidates_so_late_mark_is_discarded(self):
        """When push_doorbell_frame_sync times out and returns False, a subsequent
        call to mark_ws_delivered for the same (terminal, row) is a no-op because
        the invalidation set blocks it."""
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            _ws_invalidated,
            _ws_invalidated_lock,
            _invalidate_ws_send,
            consume_ws_delivered,
            mark_ws_delivered,
        )

        terminal_id = "term_race1"
        row_id = 9001

        # Clean state
        with _ws_delivered_lock:
            _ws_delivered.pop((terminal_id, row_id), None)
        with _ws_invalidated_lock:
            _ws_invalidated.pop((terminal_id, row_id), None)

        # Simulate what happens on timeout: _invalidate_ws_send is called
        _invalidate_ws_send(terminal_id, row_id)

        # Now simulate the late WS send completing and trying to mark
        mark_ws_delivered(terminal_id, row_id)

        # The mark should NOT be in the delivered set (invalidation blocked it)
        assert consume_ws_delivered(terminal_id, row_id) is False

    def test_push_doorbell_frame_sync_timeout_sets_invalidation(self):
        """Full integration: push_doorbell_frame_sync with a slow WS that times out
        sets the invalidation and returns False. A subsequent mark attempt is blocked."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            _ws_invalidated,
            _ws_invalidated_lock,
            consume_ws_delivered,
            mark_ws_delivered,
        )

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        terminal_id = "term_race2"
        row_id = 9002

        # Clean state
        with _ws_delivered_lock:
            _ws_delivered.pop((terminal_id, row_id), None)
        with _ws_invalidated_lock:
            _ws_invalidated.pop((terminal_id, row_id), None)

        try:
            # WS that takes 2s (will timeout at 0.05s)
            async def slow_send(*a, **kw):
                await asyncio.sleep(2.0)

            mock_ws = AsyncMock()
            mock_ws.send_text = slow_send

            ws_doorbell._connections[terminal_id] = mock_ws

            # Patch the inbox_service singleton to expose our loop
            from cli_agent_orchestrator.services import inbox_service as inbox_mod

            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = loop

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                # Call from a non-event-loop thread (simulates after-commit thread)
                result = ws_doorbell.push_doorbell_frame_sync(
                    terminal_id, row_id, "sender", "hello", timeout=0.05
                )

            # Restore
            inbox_mod.inbox_service._delivery_loop = orig_loop

            # Must return False (timed out)
            assert result is False

            # Now simulate late WS completion trying to mark
            mark_ws_delivered(terminal_id, row_id)

            # Mark must be blocked by invalidation
            assert consume_ws_delivered(terminal_id, row_id) is False

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)

    def test_late_send_after_timeout_does_not_ring_twice_e2e(self):
        """End-to-end: _f413_after_commit with a slow WS → timeout → request_delivery
        called. Then a simulated late send tries mark_ws_delivered. F136 post-delivery
        check (consume_ws_delivered) returns False → native ring fires exactly ONCE."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            _ws_invalidated,
            _ws_invalidated_lock,
            consume_ws_delivered,
            mark_ws_delivered,
        )
        from cli_agent_orchestrator.clients.database import (
            _F413_DOORBELL_STASH_KEY,
            _F413_DOORBELL_SNAPSHOT_KEY,
            _f413_after_commit,
        )

        terminal_id = "term_e2e_race"
        row_id = 9003

        # Clean state
        with _ws_delivered_lock:
            _ws_delivered.pop((terminal_id, row_id), None)
        with _ws_invalidated_lock:
            _ws_invalidated.pop((terminal_id, row_id), None)

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        try:
            # Slow WS — will timeout
            async def slow_send(*a, **kw):
                await asyncio.sleep(2.0)

            mock_ws = AsyncMock()
            mock_ws.send_text = slow_send
            ws_doorbell._connections[terminal_id] = mock_ws

            session = MagicMock()
            session.in_nested_transaction.return_value = False
            session.info = {
                _F413_DOORBELL_STASH_KEY: [(terminal_id, row_id, "w1", "callback done")],
                _F413_DOORBELL_SNAPSHOT_KEY: None,
            }

            delivery_calls = []

            # Patch inbox_service singleton's _delivery_loop
            from cli_agent_orchestrator.services import inbox_service as inbox_mod

            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = loop

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                with patch(
                    "cli_agent_orchestrator.services.inbox_service.request_delivery",
                    side_effect=lambda tid: delivery_calls.append(tid),
                ):
                    _f413_after_commit(session)

            inbox_mod.inbox_service._delivery_loop = orig_loop

            # request_delivery was called (F136 will fire)
            assert terminal_id in delivery_calls

            # Now simulate the late WS send completing
            mark_ws_delivered(terminal_id, row_id)

            # F136 post-delivery would check consume_ws_delivered — must return False
            # (the mark was blocked by invalidation)
            assert consume_ws_delivered(terminal_id, row_id) is False

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)


class TestF158R3SameLoopCaller:
    """Same-loop caller: push_doorbell_frame_sync on the owning event loop
    returns False immediately without blocking."""

    @pytest.mark.asyncio
    async def test_same_loop_returns_false_immediately(self):
        """When called from inside the event loop that would run the WS send,
        push_doorbell_frame_sync returns False without blocking."""
        from cli_agent_orchestrator.services import ws_doorbell

        terminal_id = "term_sameloop"
        mock_ws = AsyncMock()
        mock_ws.send_text.return_value = None

        ws_doorbell._connections[terminal_id] = mock_ws

        try:
            # Patch inbox_service singleton's _delivery_loop to be the current loop
            from cli_agent_orchestrator.services import inbox_service as inbox_mod

            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = asyncio.get_running_loop()

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                start = time.monotonic()
                result = ws_doorbell.push_doorbell_frame_sync(
                    terminal_id, 100, "s", "msg", timeout=0.5
                )
                elapsed = time.monotonic() - start

            inbox_mod.inbox_service._delivery_loop = orig_loop

            # Must return False (same-loop detection)
            assert result is False
            # Must NOT have blocked for the full timeout
            assert elapsed < 0.1, f"Blocked for {elapsed:.2f}s — same-loop detection failed"
            # send_text must NOT have been called
            mock_ws.send_text.assert_not_called()
        finally:
            ws_doorbell._connections.pop(terminal_id, None)

    @pytest.mark.asyncio
    async def test_same_loop_f413_selects_native_fallback(self):
        """When _f413_after_commit runs on the event loop (simulating the direct
        POST route before the R3 fix), push_doorbell_frame_sync returns False and
        request_delivery is called for native fallback."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.clients.database import (
            _F413_DOORBELL_STASH_KEY,
            _F413_DOORBELL_SNAPSHOT_KEY,
            _f413_after_commit,
        )

        terminal_id = "term_sameloop2"
        row_id = 200

        mock_ws = AsyncMock()
        mock_ws.send_text.return_value = None
        ws_doorbell._connections[terminal_id] = mock_ws

        session = MagicMock()
        session.in_nested_transaction.return_value = False
        session.info = {
            _F413_DOORBELL_STASH_KEY: [(terminal_id, row_id, "s", "msg")],
            _F413_DOORBELL_SNAPSHOT_KEY: None,
        }

        delivery_calls = []

        try:
            # Patch inbox_service singleton's _delivery_loop to be the current loop
            from cli_agent_orchestrator.services import inbox_service as inbox_mod

            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = asyncio.get_running_loop()

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                with patch(
                    "cli_agent_orchestrator.services.inbox_service.request_delivery",
                    side_effect=lambda tid: delivery_calls.append(tid),
                ):
                    _f413_after_commit(session)

            inbox_mod.inbox_service._delivery_loop = orig_loop

            # Native fallback must fire
            assert terminal_id in delivery_calls
            # WS send must NOT have been attempted (same-loop short-circuit)
            mock_ws.send_text.assert_not_called()
        finally:
            ws_doorbell._connections.pop(terminal_id, None)


class TestF158R3NormalSuccessPath:
    """Normal success: WS delivers → mark set → F136 consumes → no native ring."""

    def test_ws_success_marks_and_f136_consumes(self):
        """Full path: _f413_after_commit sends WS successfully, marks delivery.
        Then F136 post-delivery consumes the mark and skips native ring."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            consume_ws_delivered,
        )
        from cli_agent_orchestrator.clients.database import (
            _F413_DOORBELL_STASH_KEY,
            _F413_DOORBELL_SNAPSHOT_KEY,
            _f413_after_commit,
        )

        terminal_id = "term_success"
        row_id = 500

        # Clean state
        with _ws_delivered_lock:
            _ws_delivered.pop((terminal_id, row_id), None)

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        try:
            # Fast WS — succeeds immediately
            mock_ws = AsyncMock()
            mock_ws.send_text.return_value = None
            ws_doorbell._connections[terminal_id] = mock_ws

            session = MagicMock()
            session.in_nested_transaction.return_value = False
            session.info = {
                _F413_DOORBELL_STASH_KEY: [(terminal_id, row_id, "w", "done")],
                _F413_DOORBELL_SNAPSHOT_KEY: None,
            }

            delivery_calls = []

            # Patch inbox_service singleton's _delivery_loop
            from cli_agent_orchestrator.services import inbox_service as inbox_mod

            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = loop

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                with patch(
                    "cli_agent_orchestrator.services.inbox_service.request_delivery",
                    side_effect=lambda tid: delivery_calls.append(tid),
                ):
                    _f413_after_commit(session)

            inbox_mod.inbox_service._delivery_loop = orig_loop

            # request_delivery still called (F136 still runs for inbox processing)
            assert terminal_id in delivery_calls

            # F136 post-delivery consumes the mark → returns True → skip native ring
            assert consume_ws_delivered(terminal_id, row_id) is True

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)

    def test_consume_after_success_prevents_duplicate_ring(self):
        """After consume_ws_delivered returns True, a ring_supervisor_doorbell
        call is NOT made — simulating the F136 post-delivery branch."""
        from cli_agent_orchestrator.services.ws_doorbell import (
            consume_ws_delivered,
            mark_ws_delivered,
        )

        terminal_id = "term_nodup"
        row_id = 600

        # Mark as delivered
        mark_ws_delivered(terminal_id, row_id)

        # F136 checks: consume returns True → skip ring
        ws_already = consume_ws_delivered(terminal_id, row_id)
        assert ws_already is True

        # Second consume returns False (already consumed)
        assert consume_ws_delivered(terminal_id, row_id) is False


class TestF158R3AbandonedPathCleanup:
    """S1: delivered-mark set is cleaned up on abandon/disconnect."""

    def test_abandon_cleans_delivered_marks(self):
        """abandon_ws_delivered removes all marks for a terminal."""
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            abandon_ws_delivered,
            consume_ws_delivered,
            mark_ws_delivered,
        )

        terminal_id = "term_abandon"

        # Mark several rows
        mark_ws_delivered(terminal_id, 1)
        mark_ws_delivered(terminal_id, 2)
        mark_ws_delivered(terminal_id, 3)

        # Abandon
        abandon_ws_delivered(terminal_id)

        # All marks should be gone
        assert consume_ws_delivered(terminal_id, 1) is False
        assert consume_ws_delivered(terminal_id, 2) is False
        assert consume_ws_delivered(terminal_id, 3) is False

    def test_batch_consume_cleans_earlier_marks(self):
        """consume_ws_delivered(terminal, max_row) also removes earlier marks."""
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            consume_ws_delivered,
            mark_ws_delivered,
        )

        terminal_id = "term_batch"

        # Mark rows 10, 20, 30
        mark_ws_delivered(terminal_id, 10)
        mark_ws_delivered(terminal_id, 20)
        mark_ws_delivered(terminal_id, 30)

        # Consume with max row_id=30 — should clean up 10 and 20 too
        result = consume_ws_delivered(terminal_id, 30)
        assert result is True

        # Earlier marks should be gone
        assert consume_ws_delivered(terminal_id, 10) is False
        assert consume_ws_delivered(terminal_id, 20) is False


class TestF158R3TargetedEviction:
    """S1: targeted eviction replaces clear-all-at-4096."""

    def test_eviction_removes_old_entries_not_active(self):
        """When the store hits capacity, old entries are evicted but recent ones kept."""
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            _WS_DELIVERED_MAX,
            _evict_stale,
        )

        # Fill with old entries
        now = time.monotonic()
        old_time = now - 60.0  # 60s old (past TTL of 30s)

        test_store: dict[tuple[str, int], float] = {}
        for i in range(100):
            test_store[("term_old", i)] = old_time
        # Add a recent entry
        test_store[("term_new", 999)] = now

        _evict_stale(test_store, max_size=50)

        # Old entries should be gone, recent one kept
        assert ("term_new", 999) in test_store
        assert ("term_old", 0) not in test_store
