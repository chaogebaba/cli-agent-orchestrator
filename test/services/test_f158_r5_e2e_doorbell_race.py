"""F158-R5: End-to-end doorbell race tests — single-arbitration architecture.

These tests prove the R5 guarantee: push_doorbell_frame_sync WAITS for the
submitted coroutine to terminate before returning. The return value therefore
reflects the actual send outcome:
  - True = frame was emitted, WS wins, no native fallback
  - False = frame was NOT emitted, native fallback fires

The cancellation-resistant send_text test proves: even if send_text catches
CancelledError and emits the frame synchronously, the function returns True
(WS won) — the frame and native fallback NEVER coexist.

Tests exercise _f136_post_delivery with real CallbackRunOutcome via the
production doorbell_coalesce_service.submit boundary.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestF158R5SingleArbitration:
    """B1: The sync wrapper arbitrates WS/native winner by waiting for
    the coroutine to terminate. Late frame + native fallback never coexist."""

    def test_cancellation_resistant_send_ws_wins(self):
        """A send_text that catches CancelledError and emits synchronously:
        push_doorbell_frame_sync returns True (WS wins) — NOT False.
        This guarantees no native fallback fires after the frame."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
        )

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        terminal_id = "term_resist_r5"
        row_id = 5001
        frames_emitted: list[str] = []

        with _ws_delivered_lock:
            _ws_delivered.pop((terminal_id, row_id), None)

        try:
            # Cancellation-resistant send_text: catches CancelledError,
            # emits frame SYNCHRONOUSLY (no await after catch).
            async def resistant_send_text(text):
                try:
                    await asyncio.sleep(2.0)  # will be cancelled
                except asyncio.CancelledError:
                    pass  # resist cancellation
                # Synchronous emission — no await, so no second CancelledError
                frames_emitted.append(text)

            mock_ws = AsyncMock()
            mock_ws.send_text = resistant_send_text
            ws_doorbell._connections[terminal_id] = mock_ws

            from cli_agent_orchestrator.services import inbox_service as inbox_mod

            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = loop

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                result = ws_doorbell.push_doorbell_frame_sync(
                    terminal_id, row_id, "sender", "hello", timeout=0.05
                )

            inbox_mod.inbox_service._delivery_loop = orig_loop

            # The frame WAS emitted (send_text resisted cancellation)
            assert len(frames_emitted) == 1
            # R5 guarantee: function returns True because WS won
            # (the frame was emitted, so native fallback must NOT fire)
            assert result is True, (
                "push_doorbell_frame_sync must return True when send_text emitted "
                "the frame — WS wins, no native fallback"
            )

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)

    def test_cancelled_send_native_wins(self):
        """A send_text that propagates CancelledError (normal Starlette behavior):
        push_doorbell_frame_sync returns False — native fallback fires."""
        from cli_agent_orchestrator.services import ws_doorbell

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        terminal_id = "term_cancel_r5"
        row_id = 5002
        frames_emitted: list[str] = []

        try:
            # Normal send_text: propagates CancelledError (doesn't catch it)
            async def normal_send_text(text):
                await asyncio.sleep(2.0)  # will be cancelled, propagates
                frames_emitted.append(text)  # never reached

            mock_ws = AsyncMock()
            mock_ws.send_text = normal_send_text
            ws_doorbell._connections[terminal_id] = mock_ws

            from cli_agent_orchestrator.services import inbox_service as inbox_mod

            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = loop

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                result = ws_doorbell.push_doorbell_frame_sync(
                    terminal_id, row_id, "sender", "hello", timeout=0.05
                )

            inbox_mod.inbox_service._delivery_loop = orig_loop

            # No frame was emitted (CancelledError propagated)
            assert len(frames_emitted) == 0
            # Function returns False — native fallback fires
            assert result is False

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)

    def test_permit_blocks_send_before_start(self):
        """When the coroutine hasn't reached send_text before timeout fires,
        the permit prevents the send. Function returns False."""
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            _guarded_push_doorbell_frame,
        )

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        terminal_id = "term_permit_r5"
        row_id = 5003
        send_calls = []

        try:

            async def tracked_send(text):
                send_calls.append(text)

            mock_ws = AsyncMock()
            mock_ws.send_text = tracked_send
            ws_doorbell._connections[terminal_id] = mock_ws

            # Test the coroutine directly with a pre-cleared permit
            permit = threading.Event()
            permit.clear()  # Already cleared

            import concurrent.futures

            fut = asyncio.run_coroutine_threadsafe(
                _guarded_push_doorbell_frame(terminal_id, row_id, "s", "p", permit),
                loop,
            )
            result = fut.result(timeout=2.0)

            assert result is False
            assert len(send_calls) == 0  # send_text never called

        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)

    def test_no_coexistence_frame_and_fallback(self):
        """F476 r3 (#388): the WS frame and native fallback NEVER coexist for one
        id. Under r3 the WS frame is fired inside the cursor-gated runner (not the
        _f413_after_commit insert hook), and its result is carried on
        outcome._ws_fired. _f136_post_delivery submits the native ring ONLY when
        _ws_fired is False. This asserts: _ws_fired=True → zero coalesce submits."""
        from cli_agent_orchestrator.services.inbox_service import (
            CallbackRunOutcome,
            inbox_service,
        )

        terminal_id = "term_nocoexist"
        row_id = 5004
        coalesce_calls: list = []

        # The runner reports the WS frame already fired for this id.
        outcome = CallbackRunOutcome(
            written=1,
            max_written_row_id=row_id,
            reason="ok",
            _ws_fired=True,
        )

        with patch(
            "cli_agent_orchestrator.services.doorbell_coalesce.doorbell_coalesce_service.submit",
            side_effect=lambda *a, **kw: coalesce_calls.append((a, kw)),
        ):
            inbox_service._f136_post_delivery(terminal_id, outcome)

        # WS won → native ring suppressed (frame + fallback never coexist).
        assert len(coalesce_calls) == 0


class TestF158R5RealF136PostDelivery:
    """S2: Tests that exercise real _f136_post_delivery with CallbackRunOutcome
    and observe the doorbell_coalesce_service.submit boundary."""

    def test_ws_delivered_suppresses_coalesce_submit(self):
        """F476 r3 (#388): when the runner fired the WS frame (outcome._ws_fired
        =True), _f136_post_delivery suppresses the native coalesce submit — at
        most one wake transport per id."""
        from cli_agent_orchestrator.services.inbox_service import (
            CallbackRunOutcome,
            inbox_service,
        )

        terminal_id = "term_f136_ws_r5"
        row_id = 6001

        # r3: the WS-fired signal is carried on the outcome, not a side-table.
        outcome = CallbackRunOutcome(
            written=1, max_written_row_id=row_id, reason="ok", _ws_fired=True
        )
        submit_calls = []
        with patch(
            "cli_agent_orchestrator.services.doorbell_coalesce.doorbell_coalesce_service.submit",
            side_effect=lambda *a, **kw: submit_calls.append((a, kw)),
        ):
            inbox_service._f136_post_delivery(terminal_id, outcome)

        assert len(submit_calls) == 0

    def test_no_mark_triggers_coalesce_submit(self):
        """No WS mark + _f136_post_delivery → coalesce submit fires."""
        from cli_agent_orchestrator.services.inbox_service import (
            CallbackRunOutcome,
            inbox_service,
        )
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
        )

        terminal_id = "term_f136_native_r5"
        row_id = 6002

        with _ws_delivered_lock:
            _ws_delivered.pop((terminal_id, row_id), None)

        outcome = CallbackRunOutcome(written=1, max_written_row_id=row_id, reason="ok")
        submit_calls = []
        with patch(
            "cli_agent_orchestrator.services.doorbell_coalesce.doorbell_coalesce_service.submit",
            side_effect=lambda *a, **kw: submit_calls.append((a, kw)),
        ):
            inbox_service._f136_post_delivery(terminal_id, outcome)

        assert len(submit_calls) == 1
        assert submit_calls[0][0][0] == terminal_id


class TestF158R5SameLoopCaller:
    """Same-loop caller returns False immediately."""

    @pytest.mark.asyncio
    async def test_same_loop_returns_false(self):
        from cli_agent_orchestrator.services import ws_doorbell

        terminal_id = "term_sameloop_r5"
        mock_ws = AsyncMock()
        ws_doorbell._connections[terminal_id] = mock_ws

        try:
            from cli_agent_orchestrator.services import inbox_service as inbox_mod

            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = asyncio.get_running_loop()

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                result = ws_doorbell.push_doorbell_frame_sync(
                    terminal_id, 100, "s", "msg", timeout=0.5
                )

            inbox_mod.inbox_service._delivery_loop = orig_loop
            assert result is False
            mock_ws.send_text.assert_not_called()
        finally:
            ws_doorbell._connections.pop(terminal_id, None)


class TestF158R5S1StateLifecycle:
    """S1: TTL expiry and conditional abandon."""

    def test_expired_mark_not_consumed(self):
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            _WS_ENTRY_TTL,
            consume_ws_delivered,
        )

        terminal_id, row_id = "term_expire_r5", 9001
        with _ws_delivered_lock:
            _ws_delivered[(terminal_id, row_id)] = time.monotonic() - _WS_ENTRY_TTL - 5.0
        assert consume_ws_delivered(terminal_id, row_id) is False

    def test_superseded_disconnect_preserves_marks(self):
        from cli_agent_orchestrator.services import ws_doorbell
        from cli_agent_orchestrator.services.ws_doorbell import (
            consume_ws_delivered,
            mark_ws_delivered,
        )

        terminal_id = "term_supersede_r5"
        new_ws, old_ws = AsyncMock(), AsyncMock()

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        try:
            fut = asyncio.run_coroutine_threadsafe(
                ws_doorbell.register_connection(terminal_id, new_ws), loop
            )
            fut.result(timeout=2.0)
            mark_ws_delivered(terminal_id, 9002)

            fut = asyncio.run_coroutine_threadsafe(
                ws_doorbell.unregister_connection(terminal_id, old_ws), loop
            )
            fut.result(timeout=2.0)

            assert consume_ws_delivered(terminal_id, 9002) is True
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop(terminal_id, None)
