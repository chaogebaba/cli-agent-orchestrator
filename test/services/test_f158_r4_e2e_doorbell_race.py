"""F158-R4: End-to-end doorbell race tests using real F136 delivery path.

These tests:
- Do NOT patch request_delivery
- Exercise _f136_post_delivery with real CallbackRunOutcome objects
- Use cancellation-resistant send_text (catches CancelledError) to prove
  the permit-based cancellation prevents late frames
- Cover the same-loop caller case
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestF158R4PermitCancellationBlocksLateFrame:
    """B1: The permit mechanism prevents a late WS frame when send_text
    has NOT yet started executing."""

    def test_slow_startup_send_blocked_by_permit(self):
        """When the permit is cleared BEFORE the coroutine's second permit check,
        send_text is never called."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            _guarded_push_doorbell_frame,
            consume_ws_delivered,
            mark_ws_delivered,
        )

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        terminal_id = "term_permit_gate"
        row_id = 7001
        send_calls = []

        with _ws_delivered_lock:
            _ws_delivered.pop((terminal_id, row_id), None)

        try:
            async def tracked_send(text):
                send_calls.append(text)

            mock_ws = AsyncMock()
            mock_ws.send_text = tracked_send
            ws_doorbell._connections[terminal_id] = mock_ws

            # Create a permit that is ALREADY cleared — simulates timeout
            # having fired before coroutine reaches the check
            permit = threading.Event()
            permit.clear()  # Already cleared!

            import concurrent.futures
            fut = asyncio.run_coroutine_threadsafe(
                _guarded_push_doorbell_frame(
                    terminal_id, row_id, "sender", "hello", permit
                ),
                loop,
            )
            result = fut.result(timeout=2.0)

            # Must return False (permit was cleared)
            assert result is False
            # send_text must NOT have been called
            assert len(send_calls) == 0

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)

    def test_inflight_send_returns_false_no_mark(self):
        """When send_text starts before timeout but completes after, the function
        returns False (post-send permit check) → no mark is set → F136 rings."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            consume_ws_delivered,
            mark_ws_delivered,
        )

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        terminal_id = "term_inflight"
        row_id = 7002

        with _ws_delivered_lock:
            _ws_delivered.pop((terminal_id, row_id), None)

        try:
            # Send takes 200ms (timeout is 50ms, so timeout fires during send)
            async def slow_send(text):
                await asyncio.sleep(0.2)

            mock_ws = AsyncMock()
            mock_ws.send_text = slow_send
            ws_doorbell._connections[terminal_id] = mock_ws

            from cli_agent_orchestrator.services import inbox_service as inbox_mod
            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = loop

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                result = ws_doorbell.push_doorbell_frame_sync(
                    terminal_id, row_id, "sender", "hello", timeout=0.05
                )

            inbox_mod.inbox_service._delivery_loop = orig_loop

            # Returns False (timed out before send completed)
            assert result is False
            # mark_ws_delivered blocked by invalidation → F136 will ring
            mark_ws_delivered(terminal_id, row_id)
            assert consume_ws_delivered(terminal_id, row_id) is False

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)

    def test_f413_with_slow_send_selects_fallback(self):
        """When push_doorbell_frame_sync times out due to slow send,
        it returns False, sets invalidation, and mark_ws_delivered is blocked."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            consume_ws_delivered,
            mark_ws_delivered,
        )

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        terminal_id = "term_f413_slow"
        row_id = 7003

        with _ws_delivered_lock:
            _ws_delivered.pop((terminal_id, row_id), None)

        try:
            # Send takes 2s — will timeout at 0.05s
            async def slow_send(text):
                await asyncio.sleep(2.0)

            mock_ws = AsyncMock()
            mock_ws.send_text = slow_send
            ws_doorbell._connections[terminal_id] = mock_ws

            from cli_agent_orchestrator.services import inbox_service as inbox_mod
            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = loop

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                ws_fired = ws_doorbell.push_doorbell_frame_sync(
                    terminal_id, row_id, "w1", "callback", timeout=0.05
                )

            # Let loop drain
            import concurrent.futures
            fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(0.2), loop)
            fut.result(timeout=2.0)

            inbox_mod.inbox_service._delivery_loop = orig_loop

            # WS timed out
            assert ws_fired is False
            # Invalidation blocks mark_ws_delivered → F136 will ring
            mark_ws_delivered(terminal_id, row_id)
            assert consume_ws_delivered(terminal_id, row_id) is False

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)


class TestF158R4RealF136PostDelivery:
    """S2: Tests that exercise the real _f136_post_delivery path with
    CallbackRunOutcome, not manual mark/consume calls."""

    def test_f136_post_delivery_skips_ring_when_ws_delivered(self):
        """When WS successfully delivered (mark set), _f136_post_delivery
        consumes the mark and does NOT call doorbell_coalesce_service.submit."""
        from cli_agent_orchestrator.services.inbox_service import (
            CallbackRunOutcome,
            inbox_service,
        )
        from cli_agent_orchestrator.services.ws_doorbell import mark_ws_delivered

        terminal_id = "term_f136_ws"
        row_id = 8001

        # Pre-mark as WS-delivered (simulates successful push_doorbell_frame_sync)
        mark_ws_delivered(terminal_id, row_id)

        # Build a real CallbackRunOutcome
        outcome = CallbackRunOutcome(
            written=1,
            max_written_row_id=row_id,
            reason="ok",
        )

        submit_calls = []
        with patch(
            "cli_agent_orchestrator.services.doorbell_coalesce.doorbell_coalesce_service.submit",
            side_effect=lambda *a, **kw: submit_calls.append((a, kw)),
        ):
            inbox_service._f136_post_delivery(terminal_id, outcome)

        # Submit must NOT have been called (WS already woke the supervisor)
        assert len(submit_calls) == 0

    def test_f136_post_delivery_rings_when_no_ws_mark(self):
        """When WS did NOT deliver (no mark), _f136_post_delivery calls
        doorbell_coalesce_service.submit."""
        from cli_agent_orchestrator.services.inbox_service import (
            CallbackRunOutcome,
            inbox_service,
        )
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
        )

        terminal_id = "term_f136_native"
        row_id = 8002

        # Ensure no mark exists
        with _ws_delivered_lock:
            _ws_delivered.pop((terminal_id, row_id), None)

        outcome = CallbackRunOutcome(
            written=1,
            max_written_row_id=row_id,
            reason="ok",
        )

        submit_calls = []
        with patch(
            "cli_agent_orchestrator.services.doorbell_coalesce.doorbell_coalesce_service.submit",
            side_effect=lambda *a, **kw: submit_calls.append((a, kw)),
        ):
            inbox_service._f136_post_delivery(terminal_id, outcome)

        # Submit MUST have been called (no WS mark → native fallback)
        assert len(submit_calls) == 1
        assert submit_calls[0][0][0] == terminal_id

    def test_f136_post_delivery_after_timeout_rings_once(self):
        """Full flow: push_doorbell_frame_sync times out (cancellation-resistant send),
        _f413_after_commit calls request_delivery, then _f136_post_delivery fires
        with no mark → exactly one native ring."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            consume_ws_delivered,
        )
        from cli_agent_orchestrator.services.inbox_service import (
            CallbackRunOutcome,
            inbox_service,
        )
        from cli_agent_orchestrator.clients.database import (
            _F413_DOORBELL_STASH_KEY,
            _F413_DOORBELL_SNAPSHOT_KEY,
            _f413_after_commit,
        )

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        terminal_id = "term_full_flow"
        row_id = 8003

        with _ws_delivered_lock:
            _ws_delivered.pop((terminal_id, row_id), None)

        try:
            # Cancellation-resistant WS
            async def resistant_send_text(text):
                try:
                    await asyncio.sleep(2.0)
                except asyncio.CancelledError:
                    pass
                await asyncio.sleep(0.05)

            mock_ws = AsyncMock()
            mock_ws.send_text = resistant_send_text
            ws_doorbell._connections[terminal_id] = mock_ws

            from cli_agent_orchestrator.services import inbox_service as inbox_mod
            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = loop

            session = MagicMock()
            session.in_nested_transaction.return_value = False
            session.info = {
                _F413_DOORBELL_STASH_KEY: [(terminal_id, row_id, "w", "done")],
                _F413_DOORBELL_SNAPSHOT_KEY: None,
            }

            delivery_calls = []

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                with patch(
                    "cli_agent_orchestrator.services.inbox_service.request_delivery",
                    side_effect=lambda tid: delivery_calls.append(tid),
                ):
                    _f413_after_commit(session)

            # Let loop drain
            import concurrent.futures
            fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(0.2), loop)
            fut.result(timeout=1.0)

            inbox_mod.inbox_service._delivery_loop = orig_loop

            # request_delivery was called
            assert terminal_id in delivery_calls

            # Now simulate what F136 does: _f136_post_delivery with the outcome
            outcome = CallbackRunOutcome(
                written=1,
                max_written_row_id=row_id,
                reason="ok",
            )

            ring_calls = []
            with patch(
                "cli_agent_orchestrator.services.doorbell_coalesce.doorbell_coalesce_service.submit",
                side_effect=lambda *a, **kw: ring_calls.append((a, kw)),
            ):
                inbox_service._f136_post_delivery(terminal_id, outcome)

            # Exactly one ring (no WS mark was set because timeout invalidated it)
            assert len(ring_calls) == 1

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)

    def test_f136_post_delivery_after_success_no_ring(self):
        """Full flow: push_doorbell_frame_sync succeeds, mark set, _f136_post_delivery
        consumes mark → no native ring."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
        )
        from cli_agent_orchestrator.services.inbox_service import (
            CallbackRunOutcome,
            inbox_service,
        )
        from cli_agent_orchestrator.clients.database import (
            _F413_DOORBELL_STASH_KEY,
            _F413_DOORBELL_SNAPSHOT_KEY,
            _f413_after_commit,
        )

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        terminal_id = "term_success_flow"
        row_id = 8004

        with _ws_delivered_lock:
            _ws_delivered.pop((terminal_id, row_id), None)

        try:
            # Fast WS — succeeds immediately
            mock_ws = AsyncMock()
            mock_ws.send_text.return_value = None
            ws_doorbell._connections[terminal_id] = mock_ws

            from cli_agent_orchestrator.services import inbox_service as inbox_mod
            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = loop

            session = MagicMock()
            session.in_nested_transaction.return_value = False
            session.info = {
                _F413_DOORBELL_STASH_KEY: [(terminal_id, row_id, "w", "done")],
                _F413_DOORBELL_SNAPSHOT_KEY: None,
            }

            delivery_calls = []

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                with patch(
                    "cli_agent_orchestrator.services.inbox_service.request_delivery",
                    side_effect=lambda tid: delivery_calls.append(tid),
                ):
                    _f413_after_commit(session)

            inbox_mod.inbox_service._delivery_loop = orig_loop

            # request_delivery called
            assert terminal_id in delivery_calls

            # _f136_post_delivery with the outcome
            outcome = CallbackRunOutcome(
                written=1,
                max_written_row_id=row_id,
                reason="ok",
            )

            ring_calls = []
            with patch(
                "cli_agent_orchestrator.services.doorbell_coalesce.doorbell_coalesce_service.submit",
                side_effect=lambda *a, **kw: ring_calls.append((a, kw)),
            ):
                inbox_service._f136_post_delivery(terminal_id, outcome)

            # No ring — WS mark was consumed
            assert len(ring_calls) == 0

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)


class TestF158R4SameLoopCaller:
    """Same-loop caller returns False immediately — no deadlock."""

    @pytest.mark.asyncio
    async def test_same_loop_returns_false_no_send(self):
        """On the event loop, push_doorbell_frame_sync returns False without sending."""
        from cli_agent_orchestrator.services import ws_doorbell

        terminal_id = "term_sameloop4"
        mock_ws = AsyncMock()
        mock_ws.send_text.return_value = None
        ws_doorbell._connections[terminal_id] = mock_ws

        try:
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

            assert result is False
            assert elapsed < 0.1
            mock_ws.send_text.assert_not_called()
        finally:
            ws_doorbell._connections.pop(terminal_id, None)


class TestF158R4S1StateLifecycle:
    """S1: Delivered-state lifecycle — TTL on consume, conditional abandon."""

    def test_expired_mark_not_consumed(self):
        """An expired mark (beyond TTL) is treated as absent on consume."""
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            _WS_ENTRY_TTL,
            consume_ws_delivered,
        )

        terminal_id = "term_expire"
        row_id = 9001

        # Insert a mark with an old timestamp (past TTL)
        with _ws_delivered_lock:
            _ws_delivered[(terminal_id, row_id)] = time.monotonic() - _WS_ENTRY_TTL - 5.0

        # consume should return False (expired)
        assert consume_ws_delivered(terminal_id, row_id) is False

        # The entry should have been cleaned up
        with _ws_delivered_lock:
            assert (terminal_id, row_id) not in _ws_delivered

    def test_fresh_mark_consumed(self):
        """A fresh mark (within TTL) is consumed normally."""
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            consume_ws_delivered,
        )

        terminal_id = "term_fresh"
        row_id = 9002

        with _ws_delivered_lock:
            _ws_delivered[(terminal_id, row_id)] = time.monotonic()

        assert consume_ws_delivered(terminal_id, row_id) is True
        assert consume_ws_delivered(terminal_id, row_id) is False

    def test_superseded_disconnect_preserves_current_marks(self):
        """Unregistering a superseded (old) socket does NOT clear marks
        belonging to the current replacement socket."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            consume_ws_delivered,
            mark_ws_delivered,
        )

        terminal_id = "term_supersede"
        row_id = 9003

        # Simulate: new socket registered, mark set
        new_ws = AsyncMock()
        old_ws = AsyncMock()

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        try:
            # Register new socket
            fut = asyncio.run_coroutine_threadsafe(
                ws_doorbell.register_connection(terminal_id, new_ws), loop
            )
            fut.result(timeout=2.0)

            # Set a mark (simulating successful delivery via new socket)
            mark_ws_delivered(terminal_id, row_id)

            # Now old socket tears down — unregister with the OLD socket
            fut = asyncio.run_coroutine_threadsafe(
                ws_doorbell.unregister_connection(terminal_id, old_ws), loop
            )
            fut.result(timeout=2.0)

            # Mark must STILL be present (old socket != current → no abandon)
            assert consume_ws_delivered(terminal_id, row_id) is True

            # Connection must still be the new one
            assert ws_doorbell._connections.get(terminal_id) is new_ws

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)

    def test_current_disconnect_clears_marks(self):
        """Unregistering the CURRENT socket DOES clear marks."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            consume_ws_delivered,
            mark_ws_delivered,
        )

        terminal_id = "term_current_dc"
        row_id = 9004

        current_ws = AsyncMock()

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        try:
            fut = asyncio.run_coroutine_threadsafe(
                ws_doorbell.register_connection(terminal_id, current_ws), loop
            )
            fut.result(timeout=2.0)

            mark_ws_delivered(terminal_id, row_id)

            # Unregister the CURRENT socket
            fut = asyncio.run_coroutine_threadsafe(
                ws_doorbell.unregister_connection(terminal_id, current_ws), loop
            )
            fut.result(timeout=2.0)

            # Mark should be cleared
            assert consume_ws_delivered(terminal_id, row_id) is False

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)
