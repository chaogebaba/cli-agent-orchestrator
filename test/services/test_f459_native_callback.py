"""F459 — Native teammate-message render: payload-carrying, worker-named callbacks.

Tests:
  1. Payload content: bridge message body = worker's actual callback text
  2. From-name: uses worker display name (not "cao-" prefixed)
  3. Truncation: bodies > 8KB are defensively truncated with tail pointer

Groups 4 and 5 (marker suppression, socket fallback) are deleted in WP-ARCH 3c
with the doorbell that owned them — see the block at the foot of the file.
"""

from __future__ import annotations

import json

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
            p = build_wake_payload("w01", row_id, message_body=f"Message {row_id}")
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
        """F790 (#647): the active body cap is now 1,500 chars (was 8KB). A body
        WITHIN the 1,500-char cap is preserved verbatim."""
        from cli_agent_orchestrator.services.cc_session_registry import build_wake_payload

        body = "A" * 1400
        payload_json = build_wake_payload("w01", 1, message_body=body)
        payload = json.loads(payload_json)
        content = payload["message"]["content"]

        assert body in content
        assert "[truncated" not in content

    def test_body_over_8kb_truncated(self):
        """F790 (#647): a non-condition body over the 1,500-char cap is truncated
        with the F790 marker. The old 8KB `_F459_MAX_BODY_BYTES` bound is now dead
        for non-condition bodies because 1,500 < 8192 fires first."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            _F790_WAKE_BODY_MAX_CHARS,
            build_wake_payload,
        )

        body = "B" * 10000
        payload_json = build_wake_payload("w01", 77, message_body=body)
        payload = json.loads(payload_json)
        content = payload["message"]["content"]

        # F790 truncation marker present
        assert "[truncated" in content
        assert "full body in the inbox digest" in content
        # Full body NOT present
        assert body not in content
        # First 1,500 chars ARE present; the 8KB run is NOT
        assert "B" * _F790_WAKE_BODY_MAX_CHARS in content
        assert "B" * (_F790_WAKE_BODY_MAX_CHARS + 1) not in content

    def test_truncation_boundary_exact(self):
        """F790 (#647): a body at exactly the 1,500-char cap is NOT truncated."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            _F790_WAKE_BODY_MAX_CHARS,
            build_wake_payload,
        )

        body = "C" * _F790_WAKE_BODY_MAX_CHARS
        payload_json = build_wake_payload("w01", 1, message_body=body)
        payload = json.loads(payload_json)
        content = payload["message"]["content"]

        assert body in content
        assert "[truncated" not in content


# ===========================================================================
# 4 + 5. WP-ARCH 3c K3: marker suppression and socket fallback are GONE
# ===========================================================================
#
# Two groups stood here, twelve arms between them, and both were about the
# doorbell's native ring rather than about the payload this file is named for.
#
# Group 4 pinned the SUPPRESSION MARKER: ``doorbell_service._mark_socket_delivered``
# wrote a trace row when a socket write succeeded, ``is_socket_delivered`` read
# it back, and ``ring_supervisor_doorbell`` was required to write exactly one
# marker per row on success and to leave the row PENDING. Group 5 pinned the
# other side — a socket failure writes NO marker, so the row is retried — and
# drove ``_attempt_native_ring`` to show the body and display name reach the
# wire.
#
# The marker existed because the ring was FIRE-AND-FORGET: nothing durable
# recorded that a row had already gone out over the socket, so a second ring
# would re-present it and the trace row was the only dedupe available. The
# delivery queue removes the premise. A row's attempt state is a column the
# store owns, taken under a lease and advanced in the same transaction as the
# emit, so "already delivered over the socket" is a fact about the row rather
# than a marker written beside it. ``grep -rn socket_delivered src/`` returns
# nothing — there is no marker to write, read, or count.
#
# Their replacement is ``test/adapters/test_queue_store.py`` for the lease and
# attempt accounting, and ``test/app/delivery/test_seat_wake.py`` for what the
# tick does with a refused emission — both asserting a durable transition rather
# than the presence of a trace row.
#
# Groups 1-3 above SURVIVE untouched: ``build_wake_payload`` is a
# ``cc_session_registry`` function, not a doorbell one, and the carrier still
# renders a worker-named, payload-carrying wake.
