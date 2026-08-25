"""F459 — Native teammate-message render: payload-carrying, worker-named callbacks.

Tests:
  1. Payload content: bridge message body = worker's actual callback text
  2. From-name: uses worker display name (not "cao-" prefixed)
  3. Truncation: bodies > 8KB are defensively truncated with tail pointer
  4. Marker suppression: socket-delivered rows are recorded in trace
  5. Fallback on socket failure: socket failure → no marker, row stays pending
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ===========================================================================
# 1. Payload content: build_wake_payload carries actual message body
# ===========================================================================


class TestF459PayloadContent:
    """Bridge message body = the worker's actual callback text."""

    def test_message_body_embedded_in_payload(self):
        """When message_body is provided, it appears in the bridge payload content."""
        from cli_agent_orchestrator.services.cc_session_registry import build_wake_payload

        worker_callback = "Task completed successfully. Branch: cao/feature-1, SHA: abc123."
        payload_json = build_wake_payload(
            "worker-01",
            42,
            message_body=worker_callback,
            sender_display_name="kiro_dev-1a24ba05",
        )
        payload = json.loads(payload_json)
        content = payload["message"]["content"]

        # The actual callback text must be in the body
        assert worker_callback in content

    def test_legacy_fallback_no_message_body(self):
        """When message_body is None, legacy fixed text is used."""
        from cli_agent_orchestrator.services.cc_session_registry import build_wake_payload

        payload_json = build_wake_payload("worker-01", 42)
        payload = json.loads(payload_json)
        content = payload["message"]["content"]

        # Legacy text pattern
        assert "Callback from" in content
        assert "message id 42" in content

    def test_summary_field_is_first_line(self):
        """The summary attribute in the XML wrapper is the first line of body."""
        from cli_agent_orchestrator.services.cc_session_registry import build_wake_payload

        body = "First line of callback\nSecond line with details"
        payload_json = build_wake_payload(
            "w01", 10, message_body=body, sender_display_name="dev-w01"
        )
        payload = json.loads(payload_json)
        content = payload["message"]["content"]

        assert 'summary="First line of callback"' in content

    def test_multirow_order_preserved(self):
        """Multiple calls with ascending row IDs produce ordered payloads."""
        from cli_agent_orchestrator.services.cc_session_registry import build_wake_payload

        results = []
        for row_id in [10, 11, 12]:
            p = build_wake_payload(
                "w01", row_id, message_body=f"Message {row_id}"
            )
            results.append(json.loads(p))

        # Each payload references its own row content
        assert "Message 10" in results[0]["message"]["content"]
        assert "Message 11" in results[1]["message"]["content"]
        assert "Message 12" in results[2]["message"]["content"]


# ===========================================================================
# 2. From-name: worker display name (not "cao-" prefixed)
# ===========================================================================


class TestF459FromName:
    """from-name attribute = the WORKER's display name."""

    def test_from_name_is_worker_display_name(self):
        """from-name uses sender_display_name directly, not 'cao-' prefixed."""
        from cli_agent_orchestrator.services.cc_session_registry import build_wake_payload

        payload_json = build_wake_payload(
            "1a24ba05",
            99,
            message_body="hello",
            sender_display_name="kiro_dev-1a24ba05",
        )
        payload = json.loads(payload_json)
        content = payload["message"]["content"]

        # from-name must be the display name, not "cao-<name>"
        assert 'from-name="kiro_dev-1a24ba05"' in content
        assert 'from-name="cao-' not in content

    def test_from_name_fallback_to_worker_name_when_no_display(self):
        """When sender_display_name is None, falls back to sanitized worker_name."""
        from cli_agent_orchestrator.services.cc_session_registry import build_wake_payload

        payload_json = build_wake_payload("myworker", 5, message_body="test")
        payload = json.loads(payload_json)
        content = payload["message"]["content"]

        assert 'from-name="myworker"' in content

    def test_from_name_sanitized(self):
        """Special characters in display name are sanitized."""
        from cli_agent_orchestrator.services.cc_session_registry import build_wake_payload

        payload_json = build_wake_payload(
            "w01", 1, message_body="x", sender_display_name="bad name!@#"
        )
        payload = json.loads(payload_json)
        content = payload["message"]["content"]

        # No special chars in from-name
        assert "!" not in content.split('from-name="')[1].split('"')[0]
        assert "@" not in content.split('from-name="')[1].split('"')[0]


# ===========================================================================
# 3. Truncation: bodies > 8KB truncated with tail pointer
# ===========================================================================


class TestF459Truncation:
    """Bodies exceeding 8KB are defensively truncated."""

    def test_body_under_8kb_not_truncated(self):
        """A body under 8KB is preserved verbatim."""
        from cli_agent_orchestrator.services.cc_session_registry import build_wake_payload

        body = "A" * 4000
        payload_json = build_wake_payload("w01", 1, message_body=body)
        payload = json.loads(payload_json)
        content = payload["message"]["content"]

        assert body in content
        assert "[truncated" not in content

    def test_body_over_8kb_truncated(self):
        """A body over 8KB is truncated with a tail pointer."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            _F459_MAX_BODY_BYTES,
            build_wake_payload,
        )

        body = "B" * 10000
        payload_json = build_wake_payload("w01", 77, message_body=body)
        payload = json.loads(payload_json)
        content = payload["message"]["content"]

        # Truncated indicator present
        assert "[truncated" in content
        assert "inbox row 77" in content
        # Full body NOT present
        assert body not in content
        # First 8KB IS present
        assert "B" * _F459_MAX_BODY_BYTES in content

    def test_truncation_boundary_exact(self):
        """A body at exactly 8KB is NOT truncated."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            _F459_MAX_BODY_BYTES,
            build_wake_payload,
        )

        body = "C" * _F459_MAX_BODY_BYTES
        payload_json = build_wake_payload("w01", 1, message_body=body)
        payload = json.loads(payload_json)
        content = payload["message"]["content"]

        assert body in content
        assert "[truncated" not in content


# ===========================================================================
# 4. Marker suppression: socket-delivered rows recorded in trace
# ===========================================================================


class TestF459MarkerSuppression:
    """Socket-delivered rows are marked so drain hook skips them."""

    def test_mark_socket_delivered_creates_trace(self):
        """_mark_socket_delivered writes an f459.socket_delivered trace event."""
        from cli_agent_orchestrator.services.doorbell_service import (
            _mark_socket_delivered,
            is_socket_delivered,
        )

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_session)
        mock_db.__exit__ = MagicMock(return_value=False)

        with patch(
            "cli_agent_orchestrator.clients.database.SessionLocal",
            return_value=mock_db,
        ):
            _mark_socket_delivered(42)

        # Verify a trace event was added
        mock_session.add.assert_called_once()
        added = mock_session.add.call_args[0][0]
        assert added.kind == "f459.socket_delivered"
        assert added.message_id == 42
        mock_session.commit.assert_called_once()

    def test_is_socket_delivered_true(self):
        """is_socket_delivered returns True when trace exists."""
        from cli_agent_orchestrator.services.doorbell_service import is_socket_delivered

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_session)
        mock_db.__exit__ = MagicMock(return_value=False)
        # Simulate query returning a result
        mock_session.query.return_value.filter.return_value.first.return_value = (1,)

        with patch(
            "cli_agent_orchestrator.clients.database.SessionLocal",
            return_value=mock_db,
        ):
            result = is_socket_delivered(42)

        assert result is True

    def test_is_socket_delivered_false(self):
        """is_socket_delivered returns False when no trace exists."""
        from cli_agent_orchestrator.services.doorbell_service import is_socket_delivered

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_session)
        mock_db.__exit__ = MagicMock(return_value=False)
        # Simulate query returning None
        mock_session.query.return_value.filter.return_value.first.return_value = None

        with patch(
            "cli_agent_orchestrator.clients.database.SessionLocal",
            return_value=mock_db,
        ):
            result = is_socket_delivered(42)

        assert result is False

    def test_ring_marks_delivered_on_success(self):
        """Successful native ring calls _mark_socket_delivered."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.ConfigService.get",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._get_last_doorbell_row_id",
                return_value=0,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._is_row_still_pending",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._attempt_native_ring",
                return_value="rang",
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._persist_last_doorbell_row_id",
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._mark_socket_delivered",
            ) as mock_mark,
        ):
            result = ring_supervisor_doorbell(
                "term-01", 100,
                written_count=1,
                message_body="hello world",
                sender_display_name="kiro_dev-abc123",
            )

        assert result == "rang"
        mock_mark.assert_called_once_with(100)

    def test_ring_no_marker_without_message_body(self):
        """No marker when message_body is None (legacy path)."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.ConfigService.get",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._get_last_doorbell_row_id",
                return_value=0,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._is_row_still_pending",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._attempt_native_ring",
                return_value="rang",
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._persist_last_doorbell_row_id",
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._mark_socket_delivered",
            ) as mock_mark,
        ):
            result = ring_supervisor_doorbell(
                "term-01", 100,
                written_count=1,
                # No message_body — legacy call
            )

        assert result == "rang"
        mock_mark.assert_not_called()


# ===========================================================================
# 5. Fallback on socket failure: no marker, row stays pending
# ===========================================================================


class TestF459Fallback:
    """Socket write fails → row NOT marked, fallback path unchanged."""

    def test_socket_failure_no_marker(self):
        """When native ring fails, _mark_socket_delivered is NOT called."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.ConfigService.get",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._get_last_doorbell_row_id",
                return_value=0,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._is_row_still_pending",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._attempt_native_ring",
                return_value="socket_econnrefused",
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._attempt_gated_ring",
                return_value="rang",
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._persist_last_doorbell_row_id",
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._mark_socket_delivered",
            ) as mock_mark,
        ):
            result = ring_supervisor_doorbell(
                "term-01", 100,
                written_count=1,
                message_body="hello",
                sender_display_name="dev-x",
            )

        # Fell back to pane nudge
        assert result == "fallback"
        # No socket-delivered marker
        mock_mark.assert_not_called()

    def test_native_ring_passes_body_and_name(self):
        """_attempt_native_ring receives message_body and sender_display_name kwargs."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.ConfigService.get",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._get_last_doorbell_row_id",
                return_value=0,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._is_row_still_pending",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._attempt_native_ring",
            ) as mock_native,
            patch(
                "cli_agent_orchestrator.services.doorbell_service._persist_last_doorbell_row_id",
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._mark_socket_delivered",
            ),
        ):
            mock_native.return_value = "rang"
            ring_supervisor_doorbell(
                "term-01", 100,
                written_count=1,
                message_body="callback text here",
                sender_display_name="kiro_dev-abc",
            )

        mock_native.assert_called_once_with(
            "term-01", 100,
            message_body="callback text here",
            sender_display_name="kiro_dev-abc",
        )
