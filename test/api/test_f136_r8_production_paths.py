"""F136 r8 — Production call-path reachability tests.

These tests prove that the two r8 repairs (barrier cancel → request_delivery,
and set_supervisor_callback_inbox_path wiring) are reachable through PRODUCTION
code paths, not just helper-only unit tests.

Prior G7 V3 caught that a helper-only test (test_set_supervisor_callback_inbox_path_importable)
does NOT prove the function is reachable from any production caller.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import patch

import pytest


# ===========================================================================
# Repair A: cancel endpoint calls request_delivery (not deliver_pending)
# ===========================================================================


class TestRepairA_CancelEndpointCallsRequestDelivery:
    """Prove POST /barriers/cancel triggers request_delivery in production."""

    def test_cancel_endpoint_source_calls_request_delivery_not_deliver_pending(self):
        """The cancel endpoint source code references request_delivery, not deliver_pending."""
        from cli_agent_orchestrator.api.main import cancel_callback_barrier_endpoint

        source = inspect.getsource(cancel_callback_barrier_endpoint)
        assert "request_delivery" in source, (
            "cancel_callback_barrier_endpoint must call request_delivery (F136-D17)"
        )
        assert "deliver_pending" not in source, (
            "cancel_callback_barrier_endpoint must NOT call deliver_pending (legacy path)"
        )

    def test_cancel_endpoint_invokes_request_delivery_for_each_receiver(self, client):
        """Integration: POST /barriers/cancel calls request_delivery per receiver."""
        with (
            patch(
                "cli_agent_orchestrator.api.main.cancel_callback_barrier",
                return_value={
                    "id": 42,
                    "state": "CANCELLED",
                    "released": 3,
                    "receiver_ids": ["term_aaa", "term_bbb", "term_ccc"],
                },
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery"
            ) as mock_delivery,
        ):
            response = client.post("/barriers/cancel", params={"barrier_id": 42})
        assert response.status_code == 200
        assert response.json()["released"] == 3
        # Verify request_delivery was called for ALL receivers
        delivered_ids = [call.args[0] for call in mock_delivery.call_args_list]
        assert delivered_ids == ["term_aaa", "term_bbb", "term_ccc"]


# ===========================================================================
# Repair B: set_supervisor_callback_inbox_path reachable via production paths
# ===========================================================================


class TestRepairB_InboxPathProductionReachability:
    """Prove set_supervisor_callback_inbox_path is reachable via production callers."""

    def test_patch_endpoint_calls_set_supervisor_callback_inbox_path(self, client):
        """PATCH /mailboxes/{id}/inbox-path invokes set_supervisor_callback_inbox_path."""
        from cli_agent_orchestrator.services.mailbox_service import PathUpdateResult

        mock_result = PathUpdateResult(kind="updated", path_version=2)
        with patch(
            "cli_agent_orchestrator.services.mailbox_service.set_supervisor_callback_inbox_path",
            return_value=mock_result,
        ) as mock_set:
            response = client.patch(
                "/mailboxes/mb_abcd1234/inbox-path",
                json={
                    "path": "/home/user/.claude/inbox.json",
                    "terminal_id": "term_sup01",
                    "generation": 3,
                },
            )
        assert response.status_code == 200
        assert response.json() == {"kind": "updated", "path_version": 2}
        mock_set.assert_called_once_with(
            mailbox_id="mb_abcd1234",
            terminal_id="term_sup01",
            generation=3,
            path="/home/user/.claude/inbox.json",
        )

    def test_patch_endpoint_stale_authority_returns_409(self, client):
        """PATCH /mailboxes/{id}/inbox-path with stale generation → 409."""
        from cli_agent_orchestrator.services.mailbox_service import PathUpdateResult

        mock_result = PathUpdateResult(
            kind="stale_authority", path_version=1, reason="terminal_or_generation_mismatch"
        )
        with patch(
            "cli_agent_orchestrator.services.mailbox_service.set_supervisor_callback_inbox_path",
            return_value=mock_result,
        ):
            response = client.patch(
                "/mailboxes/mb_abcd1234/inbox-path",
                json={
                    "path": "/some/path",
                    "terminal_id": "term_old",
                    "generation": 1,
                },
            )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "stale_authority"

    def test_patch_endpoint_lock_timeout_returns_503(self, client):
        """PATCH /mailboxes/{id}/inbox-path with lock timeout → 503."""
        from cli_agent_orchestrator.services.mailbox_service import PathUpdateResult

        mock_result = PathUpdateResult(
            kind="retryable_failure", path_version=0, reason="lock_timeout"
        )
        with patch(
            "cli_agent_orchestrator.services.mailbox_service.set_supervisor_callback_inbox_path",
            return_value=mock_result,
        ):
            response = client.patch(
                "/mailboxes/mb_abcd1234/inbox-path",
                json={
                    "path": "/some/path",
                    "terminal_id": "term_sup01",
                    "generation": 3,
                },
            )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "retryable_failure"
        assert response.json()["detail"]["reason"] == "lock_timeout"

    def test_session_service_publication_calls_reconcile(self):
        """create_session publication lifecycle calls _reconcile_inbox_path_on_publish."""
        from cli_agent_orchestrator.services.session_service import (
            _reconcile_inbox_path_on_publish,
            create_session,
        )

        # Verify the helper exists and is async
        assert asyncio.iscoroutinefunction(_reconcile_inbox_path_on_publish)

        # Verify create_session source references _reconcile_inbox_path_on_publish
        source = inspect.getsource(create_session)
        assert "_reconcile_inbox_path_on_publish" in source, (
            "create_session must call _reconcile_inbox_path_on_publish after publication"
        )

    def test_reconcile_inbox_path_invokes_set_supervisor_callback_inbox_path(self):
        """_reconcile_inbox_path_on_publish calls set_supervisor_callback_inbox_path."""
        from cli_agent_orchestrator.services.session_service import _reconcile_inbox_path_on_publish

        with (
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_metadata",
                return_value={"metadata": {"cc_team_inbox_path": "/home/u/.claude/inbox.json"}},
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.set_supervisor_callback_inbox_path",
            ) as mock_set,
        ):
            asyncio.run(
                _reconcile_inbox_path_on_publish(
                    terminal_id="term_abc",
                    mailbox_id="mb_12345678",
                    generation=2,
                )
            )
        mock_set.assert_called_once_with(
            mailbox_id="mb_12345678",
            terminal_id="term_abc",
            generation=2,
            path="/home/u/.claude/inbox.json",
        )

    def test_reconcile_inbox_path_noop_when_no_metadata(self):
        """_reconcile_inbox_path_on_publish is a no-op when metadata absent."""
        from cli_agent_orchestrator.services.session_service import _reconcile_inbox_path_on_publish

        with (
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_metadata",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.set_supervisor_callback_inbox_path",
            ) as mock_set,
        ):
            asyncio.run(
                _reconcile_inbox_path_on_publish(
                    terminal_id="term_abc",
                    mailbox_id="mb_12345678",
                    generation=2,
                )
            )
        mock_set.assert_not_called()
