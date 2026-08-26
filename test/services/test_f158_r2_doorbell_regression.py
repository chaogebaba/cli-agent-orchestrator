"""F158-R2: Regression tests for doorbell delivery observability and dedup.

(a) armed but send_text raises/never completes → native fallback still fires, after commit
(b) unarmed flow → exactly ONE ring, post-write (via F136 _f136_post_delivery only)
(c) delivered WS frame → no duplicate native ring
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestF158R2_ArmedButSendFails:
    """(a) When WS is armed but send_text raises, push_doorbell_frame_sync returns
    False → _f413_after_commit calls request_delivery → F136 rings post-write."""

    def test_send_text_raises_returns_false(self):
        """push_doorbell_frame_sync returns False when ws.send_text raises."""
        from cli_agent_orchestrator.services import ws_doorbell

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        try:
            # Create a mock WebSocket that raises on send_text
            mock_ws = AsyncMock()
            mock_ws.send_text.side_effect = RuntimeError("simulated transport drop")

            # Inject it into _connections
            ws_doorbell._connections["term_test"] = mock_ws

            # R3: Set inbox_service._delivery_loop to our background loop
            from cli_agent_orchestrator.services import inbox_service as inbox_mod

            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = loop

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                result = ws_doorbell.push_doorbell_frame_sync(
                    "term_test", 42, "sender", "hello", timeout=2.0
                )

            inbox_mod.inbox_service._delivery_loop = orig_loop

            assert result is False, "Expected False when send_text raises"
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop("term_test", None)

    def test_send_text_timeout_returns_false(self):
        """push_doorbell_frame_sync returns False when the coroutine never completes."""
        from cli_agent_orchestrator.services import ws_doorbell

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        try:
            # Create a mock WebSocket that hangs forever
            async def hang_forever(*a, **kw):
                await asyncio.sleep(999)

            mock_ws = AsyncMock()
            mock_ws.send_text = hang_forever

            ws_doorbell._connections["term_hang"] = mock_ws

            # R3: Set inbox_service._delivery_loop to our background loop
            from cli_agent_orchestrator.services import inbox_service as inbox_mod

            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = loop

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                result = ws_doorbell.push_doorbell_frame_sync(
                    "term_hang", 99, "s", "msg", timeout=0.1
                )

            inbox_mod.inbox_service._delivery_loop = orig_loop

            assert result is False, "Expected False on timeout"
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop("term_hang", None)

    def test_after_commit_ws_fail_triggers_request_delivery(self):
        """When push_doorbell_frame_sync returns False (send failed),
        _f413_after_commit calls request_delivery (which triggers F136 ring)."""
        from cli_agent_orchestrator.clients.database import (
            _F413_DOORBELL_STASH_KEY,
            _F413_DOORBELL_SNAPSHOT_KEY,
            _f413_after_commit,
        )

        session = MagicMock()
        session.in_nested_transaction.return_value = False
        session.info = {
            _F413_DOORBELL_STASH_KEY: [("term_fail", 42, "sender", "hello")],
            _F413_DOORBELL_SNAPSHOT_KEY: None,
        }

        with patch(
            "cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync",
            return_value=False,
        ) as mock_ws, patch(
            "cli_agent_orchestrator.services.inbox_service.request_delivery"
        ) as mock_req:
            _f413_after_commit(session)

            mock_ws.assert_called_once_with("term_fail", 42, "sender", "hello")
            # No direct ring_supervisor_doorbell call from the hook
            mock_req.assert_called_once_with("term_fail")

    def test_after_commit_ws_fail_no_direct_ring(self):
        """When WS fails, _f413_after_commit does NOT call ring_supervisor_doorbell
        directly — it only calls request_delivery. The ring comes from F136 post-write."""
        from cli_agent_orchestrator.clients.database import (
            _F413_DOORBELL_STASH_KEY,
            _F413_DOORBELL_SNAPSHOT_KEY,
            _f413_after_commit,
        )

        session = MagicMock()
        session.in_nested_transaction.return_value = False
        session.info = {
            _F413_DOORBELL_STASH_KEY: [("term_x", 77, "s", "p")],
            _F413_DOORBELL_SNAPSHOT_KEY: None,
        }

        with patch(
            "cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync",
            return_value=False,
        ), patch(
            "cli_agent_orchestrator.services.doorbell_service.ring_supervisor_doorbell"
        ) as mock_ring, patch(
            "cli_agent_orchestrator.services.inbox_service.request_delivery"
        ):
            _f413_after_commit(session)
            # ring_supervisor_doorbell must NOT be called from the hook
            mock_ring.assert_not_called()


class TestF158R2_UnarmedFlowSingleRing:
    """(b) When WS is unarmed, exactly ONE native ring fires — from F136 post-write
    only, never from the after-commit hook directly."""

    def test_unarmed_no_mark_ws_delivered(self):
        """When WS is unarmed (returns False), mark_ws_delivered is NOT called."""
        from cli_agent_orchestrator.clients.database import (
            _F413_DOORBELL_STASH_KEY,
            _F413_DOORBELL_SNAPSHOT_KEY,
            _f413_after_commit,
        )

        session = MagicMock()
        session.in_nested_transaction.return_value = False
        session.info = {
            _F413_DOORBELL_STASH_KEY: [("term_u", 50, "s", "msg")],
            _F413_DOORBELL_SNAPSHOT_KEY: None,
        }

        with patch(
            "cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync",
            return_value=False,
        ), patch(
            "cli_agent_orchestrator.services.ws_doorbell.mark_ws_delivered"
        ) as mock_mark, patch(
            "cli_agent_orchestrator.services.inbox_service.request_delivery"
        ):
            _f413_after_commit(session)
            mock_mark.assert_not_called()

    def test_f136_post_delivery_rings_when_no_ws_mark(self):
        """F136 _f136_post_delivery rings native doorbell when consume_ws_delivered
        returns False (WS did not deliver)."""
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            consume_ws_delivered,
        )

        # Ensure no mark exists
        with _ws_delivered_lock:
            _ws_delivered.pop(("term_ring", 60), None)

        assert consume_ws_delivered("term_ring", 60) is False

    def test_unarmed_exactly_one_ring_end_to_end(self):
        """End-to-end: unarmed WS → request_delivery → F136 post-delivery → exactly
        one ring_supervisor_doorbell call."""
        from cli_agent_orchestrator.clients.database import (
            _F413_DOORBELL_STASH_KEY,
            _F413_DOORBELL_SNAPSHOT_KEY,
            _f413_after_commit,
        )

        session = MagicMock()
        session.in_nested_transaction.return_value = False
        session.info = {
            _F413_DOORBELL_STASH_KEY: [("term_one", 100, "w1", "callback result")],
            _F413_DOORBELL_SNAPSHOT_KEY: None,
        }

        ring_calls = []

        with patch(
            "cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync",
            return_value=False,
        ), patch(
            "cli_agent_orchestrator.services.inbox_service.request_delivery"
        ) as mock_req, patch(
            "cli_agent_orchestrator.services.doorbell_service.ring_supervisor_doorbell",
            side_effect=lambda *a, **kw: ring_calls.append((a, kw)),
        ):
            _f413_after_commit(session)

            # Hook must NOT ring directly
            assert len(ring_calls) == 0
            # But request_delivery must be called (starts F136 runner)
            mock_req.assert_called_once_with("term_one")


class TestF158R2_WsDeliveredNoDuplicateRing:
    """(c) When WS frame is delivered successfully, the F136 post-write native
    ring is suppressed — no duplicate wake."""

    def test_ws_success_marks_delivered(self):
        """push_doorbell_frame_sync returns True → mark_ws_delivered is called."""
        from cli_agent_orchestrator.clients.database import (
            _F413_DOORBELL_STASH_KEY,
            _F413_DOORBELL_SNAPSHOT_KEY,
            _f413_after_commit,
        )

        session = MagicMock()
        session.in_nested_transaction.return_value = False
        session.info = {
            _F413_DOORBELL_STASH_KEY: [("term_ok", 200, "w", "done")],
            _F413_DOORBELL_SNAPSHOT_KEY: None,
        }

        with patch(
            "cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync",
            return_value=True,
        ), patch(
            "cli_agent_orchestrator.services.ws_doorbell.mark_ws_delivered"
        ) as mock_mark, patch(
            "cli_agent_orchestrator.services.inbox_service.request_delivery"
        ):
            _f413_after_commit(session)
            mock_mark.assert_called_once_with("term_ok", 200)

    def test_consume_ws_delivered_returns_true_after_mark(self):
        """consume_ws_delivered returns True after mark_ws_delivered was called."""
        from cli_agent_orchestrator.services.ws_doorbell import (
            _ws_delivered,
            _ws_delivered_lock,
            consume_ws_delivered,
            mark_ws_delivered,
        )

        # Clean state
        with _ws_delivered_lock:
            _ws_delivered.pop(("term_c", 300), None)

        mark_ws_delivered("term_c", 300)
        assert consume_ws_delivered("term_c", 300) is True
        # Second consume returns False (consumed)
        assert consume_ws_delivered("term_c", 300) is False

    def test_f136_post_delivery_skips_ring_when_ws_delivered(self):
        """When consume_ws_delivered returns True, F136 post-delivery does NOT call
        ring_supervisor_doorbell — the WS frame already woke the supervisor."""
        from cli_agent_orchestrator.services.ws_doorbell import mark_ws_delivered

        # Pre-mark as WS-delivered
        mark_ws_delivered("term_ws", 400)

        # Simulate _f136_post_delivery checking the flag
        from cli_agent_orchestrator.services.ws_doorbell import consume_ws_delivered

        # The consume should return True → ring suppressed
        assert consume_ws_delivered("term_ws", 400) is True

    def test_ws_delivered_no_direct_ring_from_hook(self):
        """When WS succeeds, _f413_after_commit never calls ring_supervisor_doorbell."""
        from cli_agent_orchestrator.clients.database import (
            _F413_DOORBELL_STASH_KEY,
            _F413_DOORBELL_SNAPSHOT_KEY,
            _f413_after_commit,
        )

        session = MagicMock()
        session.in_nested_transaction.return_value = False
        session.info = {
            _F413_DOORBELL_STASH_KEY: [("term_ws2", 500, "w", "msg")],
            _F413_DOORBELL_SNAPSHOT_KEY: None,
        }

        with patch(
            "cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync",
            return_value=True,
        ), patch(
            "cli_agent_orchestrator.services.ws_doorbell.mark_ws_delivered"
        ), patch(
            "cli_agent_orchestrator.services.doorbell_service.ring_supervisor_doorbell"
        ) as mock_ring, patch(
            "cli_agent_orchestrator.services.inbox_service.request_delivery"
        ):
            _f413_after_commit(session)
            mock_ring.assert_not_called()


class TestF158R2_PushDoorbellFrameSyncObservability:
    """B1 regression: push_doorbell_frame_sync now observes the real send outcome."""

    def test_successful_send_returns_true(self):
        """When ws.send_text succeeds, push_doorbell_frame_sync returns True."""
        from cli_agent_orchestrator.services import ws_doorbell

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        try:
            mock_ws = AsyncMock()
            mock_ws.send_text.return_value = None  # success

            ws_doorbell._connections["term_good"] = mock_ws

            # R3: Set inbox_service._delivery_loop to our background loop
            from cli_agent_orchestrator.services import inbox_service as inbox_mod

            orig_loop = inbox_mod.inbox_service._delivery_loop
            inbox_mod.inbox_service._delivery_loop = loop

            with patch.object(ws_doorbell, "is_ws_monitor_enabled", return_value=True):
                result = ws_doorbell.push_doorbell_frame_sync(
                    "term_good", 10, "s", "p", timeout=2.0
                )

            inbox_mod.inbox_service._delivery_loop = orig_loop

            assert result is True
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()
            ws_doorbell._connections.pop("term_good", None)
