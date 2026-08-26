"""F461 — Doorbell coalesce service acceptance tests.

ACs:
- AC1: Single callback within window fires after coalesce_s expires (not immediately).
- AC2: N callbacks within window produce ONE ring with combined body.
- AC3: Combined ring uses from-name='cao-fleet' when N>1, individual when N=1.
- AC4: coalesce_s=0 disables coalescing (immediate fire).
- AC5: Ordering preserved — rows delivered oldest-first in combined digest.
- AC6: Flush on shutdown drains all pending buffers.
- AC7: Multiple terminals are independent (each gets its own window).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services.doorbell_coalesce import (
    DoorbellCoalesceService,
    _DoorbellIntent,
)


@pytest.fixture()
def event_loop():
    """Create an event loop for tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture()
def mock_ring():
    """Mock ring function that records calls."""
    return MagicMock(return_value="rang")


@pytest.fixture()
def service(event_loop, mock_ring):
    """Create a bound coalesce service."""
    svc = DoorbellCoalesceService()
    svc.bind(event_loop, mock_ring)
    return svc


class TestCoalesceDisabled:
    """AC4: coalesce_s=0 means immediate fire, no buffering."""

    def test_immediate_fire_when_coalesce_zero(self, service, mock_ring):
        """When coalesce_s=0, fire_fn is called inline."""
        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 0.0)):
            service.submit(
                "term1",
                100,
                written_count=1,
                message_body="hello from worker",
                sender_display_name="w1",
            )
            assert mock_ring.call_count == 1
            call_args = mock_ring.call_args
            assert call_args[0] == ("term1", 100)
            assert call_args[1]["written_count"] == 1
            assert call_args[1]["message_body"] == "hello from worker"
            assert call_args[1]["sender_display_name"] == "w1"
            assert call_args[1]["caller_holds_no_delivery_lock"] is True


class TestSingleCallback:
    """AC1: Single callback fires after window expires, not immediately."""

    def test_not_fired_immediately(self, service, mock_ring):
        """Submit does not fire ring_fn inline when coalesce_s > 0."""
        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 5.0)):
            service.submit(
                "term1",
                100,
                written_count=1,
                message_body="hello",
                sender_display_name="w1",
            )
            assert mock_ring.call_count == 0
            assert service.pending_count("term1") == 1

    def test_fires_after_window(self, service, mock_ring, event_loop):
        """After coalesce_s elapses, the ring fires with original intent."""
        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 0.05)):
            service.submit(
                "term1",
                100,
                written_count=1,
                message_body="hello",
                sender_display_name="w1",
            )
            assert mock_ring.call_count == 0

            # Run event loop briefly to let timer fire
            event_loop.run_until_complete(asyncio.sleep(0.1))

            assert mock_ring.call_count == 1
            call_args = mock_ring.call_args
            assert call_args[0] == ("term1", 100)
            assert call_args[1]["sender_display_name"] == "w1"

    def test_single_callback_preserves_display_name(self, service, mock_ring, event_loop):
        """AC3: N=1 uses individual worker's display name."""
        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 0.05)):
            service.submit(
                "term1",
                42,
                written_count=1,
                message_body="done",
                sender_display_name="my-worker",
            )
            event_loop.run_until_complete(asyncio.sleep(0.1))

            assert mock_ring.call_count == 1
            assert mock_ring.call_args[1]["sender_display_name"] == "my-worker"


class TestMultipleCallbacks:
    """AC2, AC3, AC5: N callbacks within window produce ONE coalesced ring."""

    def test_two_callbacks_coalesced(self, service, mock_ring, event_loop):
        """Two callbacks within the window become one ring."""
        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 0.1)):
            service.submit(
                "term1",
                100,
                written_count=1,
                message_body="result from w1",
                sender_display_name="worker-1",
            )
            service.submit(
                "term1",
                200,
                written_count=2,
                message_body="result from w2",
                sender_display_name="worker-2",
            )
            assert mock_ring.call_count == 0
            assert service.pending_count("term1") == 2

            event_loop.run_until_complete(asyncio.sleep(0.15))

            # One coalesced call
            assert mock_ring.call_count == 1
            call_args = mock_ring.call_args
            # AC3: from-name = 'cao-fleet' when N>1
            assert call_args[1]["sender_display_name"] == "cao-fleet"
            # Max row id across intents
            assert call_args[0][1] == 200
            # Total written count
            assert call_args[1]["written_count"] == 3
            # Combined body contains both summaries
            body = call_args[1]["message_body"]
            assert "2 callbacks coalesced" in body
            assert "worker-1" in body
            assert "worker-2" in body

    def test_ordering_preserved(self, service, mock_ring, event_loop):
        """AC5: Oldest-first ordering in combined digest."""
        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 0.1)):
            # Submit in reverse order
            service.submit(
                "term1",
                300,
                written_count=1,
                message_body="third",
                sender_display_name="w3",
            )
            service.submit(
                "term1",
                100,
                written_count=1,
                message_body="first",
                sender_display_name="w1",
            )
            service.submit(
                "term1",
                200,
                written_count=1,
                message_body="second",
                sender_display_name="w2",
            )

            event_loop.run_until_complete(asyncio.sleep(0.15))

            assert mock_ring.call_count == 1
            body = mock_ring.call_args[1]["message_body"]
            # Verify ordering: w1 (row 100) should appear before w2 (200) before w3 (300)
            w1_pos = body.index("w1")
            w2_pos = body.index("w2")
            w3_pos = body.index("w3")
            assert w1_pos < w2_pos < w3_pos

    def test_three_callbacks_from_fleet(self, service, mock_ring, event_loop):
        """Three callbacks all merge into one cao-fleet message."""
        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 0.1)):
            for i in range(3):
                service.submit(
                    "term1",
                    (i + 1) * 10,
                    written_count=1,
                    message_body=f"msg {i}",
                    sender_display_name=f"w{i}",
                )

            event_loop.run_until_complete(asyncio.sleep(0.15))

            assert mock_ring.call_count == 1
            body = mock_ring.call_args[1]["message_body"]
            assert "3 callbacks coalesced" in body


class TestMultipleTerminals:
    """AC7: Each terminal has independent coalesce buffers."""

    def test_independent_terminals(self, service, mock_ring, event_loop):
        """Two terminals accumulate and fire independently."""
        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 0.1)):
            service.submit("term1", 10, written_count=1, message_body="t1-a", sender_display_name="w1")
            service.submit("term2", 20, written_count=1, message_body="t2-a", sender_display_name="w2")
            service.submit("term1", 11, written_count=1, message_body="t1-b", sender_display_name="w3")

            event_loop.run_until_complete(asyncio.sleep(0.15))

            # Two calls: one for term1 (coalesced 2), one for term2 (single)
            assert mock_ring.call_count == 2
            calls = mock_ring.call_args_list
            term_ids = [c[0][0] for c in calls]
            assert "term1" in term_ids
            assert "term2" in term_ids

            # term1 should be coalesced (cao-fleet)
            term1_call = next(c for c in calls if c[0][0] == "term1")
            assert term1_call[1]["sender_display_name"] == "cao-fleet"
            assert "2 callbacks coalesced" in term1_call[1]["message_body"]

            # term2 should be individual
            term2_call = next(c for c in calls if c[0][0] == "term2")
            assert term2_call[1]["sender_display_name"] == "w2"


class TestFlushAll:
    """AC6: flush_all drains all pending buffers immediately."""

    def test_flush_fires_pending(self, service, mock_ring):
        """flush_all fires all pending intents without waiting for timer."""
        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 60.0)):
            service.submit("term1", 10, written_count=1, message_body="a", sender_display_name="w1")
            service.submit("term2", 20, written_count=1, message_body="b", sender_display_name="w2")

            assert mock_ring.call_count == 0

            service.flush_all()

            assert mock_ring.call_count == 2
            assert service.pending_count("term1") == 0
            assert service.pending_count("term2") == 0


class TestCallerHoldsNoDeliveryLock:
    """Verify that fire always passes caller_holds_no_delivery_lock=True."""

    def test_coalesced_passes_no_lock_flag(self, service, mock_ring, event_loop):
        """The coalesced fire call includes caller_holds_no_delivery_lock=True."""
        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 0.05)):
            service.submit("term1", 10, written_count=1, message_body="x", sender_display_name="w1")
            service.submit("term1", 20, written_count=1, message_body="y", sender_display_name="w2")

            event_loop.run_until_complete(asyncio.sleep(0.1))

            assert mock_ring.call_args[1]["caller_holds_no_delivery_lock"] is True


class TestFireFnExceptionHandling:
    """Exceptions in fire_fn are caught and logged, not propagated."""

    def test_fire_exception_swallowed(self, service, event_loop):
        """fire_fn exception doesn't crash the service."""
        bomb_ring = MagicMock(side_effect=RuntimeError("boom"))
        service.bind(event_loop, bomb_ring)

        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 0.05)):
            service.submit("term1", 10, written_count=1, message_body="x", sender_display_name="w1")
            # Should not raise
            event_loop.run_until_complete(asyncio.sleep(0.1))
            assert bomb_ring.call_count == 1


class TestNoneMessageBody:
    """Handle None message_body gracefully (pre-F459 callers)."""

    def test_none_body_single(self, service, mock_ring, event_loop):
        """None body passes through to fire_fn."""
        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 0.05)):
            service.submit("term1", 10, written_count=1, message_body=None, sender_display_name=None)
            event_loop.run_until_complete(asyncio.sleep(0.1))

            assert mock_ring.call_count == 1
            assert mock_ring.call_args[1]["message_body"] is None

    def test_none_body_coalesced(self, service, mock_ring, event_loop):
        """None body in coalesced set uses fallback preview."""
        with patch.object(type(service), "_coalesce_s", new_callable=lambda: property(lambda self: 0.05)):
            service.submit("term1", 10, written_count=1, message_body=None, sender_display_name="w1")
            service.submit("term1", 20, written_count=1, message_body="real msg", sender_display_name="w2")
            event_loop.run_until_complete(asyncio.sleep(0.1))

            assert mock_ring.call_count == 1
            body = mock_ring.call_args[1]["message_body"]
            assert "(row 10)" in body  # fallback for None body
            assert "real msg" in body
