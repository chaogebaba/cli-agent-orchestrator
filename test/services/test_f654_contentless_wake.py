"""F654 (#509) — Contentless callback wake entry acceptance tests.

D1: `write_supervisor_callback_notification` keeps its per-id call shape,
msg_id scheme, `from: "cao-bridge"`, lockfile, `already_present`, and return
kinds. ONLY the payload changes:

- text:    ``[CAO] Message <id> ready from <sender_id>. Drain: list_messages -> ack_messages``
- summary: ``Message <id> ready``
- no preview substring of the message body anywhere in the entry.

These tests pin the NEW public writer only. The legacy ``_build_entry`` /
``attempt_teammate_push`` preview shape is deliberately untouched and is pinned
by ``test/test_teammate_push_bridge.py`` (TestAC6), which must stay green.

Mixed-version note: during the first post-deploy drain, old-format entries that
survive the redeploy return ``identity_conflict`` on re-emission (``already_present``
compares text+timestamp). That is EXPECTED, not a failure — see
``test_mixed_version_old_format_survivor_is_identity_conflict``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from cli_agent_orchestrator.models.inbox import (
    InboxMessage,
    MessageStatus,
    OrchestrationType,
)
from cli_agent_orchestrator.services.teammate_push_service import (
    _TEAMMATE_FROM,
    callback_notification_id,
    write_supervisor_callback_notification,
)

_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)

# A distinctive, high-entropy token that must never leak into the wake entry.
_DISTINCTIVE_TOKEN = "ZQXJ7VKWPLMN3RTB_distinctive_payload_marker"


def _msg(
    msg_id: int = 1,
    sender: str = "worker-01",
    text: str = "done",
) -> InboxMessage:
    return InboxMessage(
        id=msg_id,
        sender_id=sender,
        receiver_id="sup-001",
        message=text,
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        status=MessageStatus.PENDING,
        created_at=_NOW,
    )


def _distinctive_payload(distinctive_token: str = _DISTINCTIVE_TOKEN) -> str:
    """A 300-char payload whose every 16-char window is distinctive."""
    body = f"{distinctive_token} " + ("abcdefghijklmnopqrstuvwxyz0123456789" * 20)
    return body[:300]


def _has_run_of_payload(haystack: str, payload: str, run_len: int = 16) -> bool:
    """True iff any >= run_len contiguous substring of payload appears in haystack."""
    for start in range(0, len(payload) - run_len + 1):
        window = payload[start : start + run_len]
        if window in haystack:
            return True
    return False


# ===========================================================================
# AC1 — Contentless
# ===========================================================================


class TestAC1Contentless:
    def test_text_and_summary_are_content_free(self, tmp_path: Path) -> None:
        """A distinctive 300-char payload leaks into neither text nor summary."""
        inbox = tmp_path / "inbox.json"
        payload = _distinctive_payload()
        assert len(payload) == 300
        msg = _msg(7, sender="kiro_dev", text=payload)

        r = write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=msg
        )
        assert r.kind == "written"

        entry = json.loads(inbox.read_text())[0]

        # No distinctive token anywhere in the entry's content fields.
        assert _DISTINCTIVE_TOKEN not in entry["text"]
        assert _DISTINCTIVE_TOKEN not in entry["summary"]

        # No >=16-char run of the payload survives in text or summary.
        assert not _has_run_of_payload(entry["text"], payload)
        assert not _has_run_of_payload(entry["summary"], payload)

    def test_text_matches_fixed_format(self, tmp_path: Path) -> None:
        """text is the fixed-format pointer carrying the correct id and sender."""
        inbox = tmp_path / "inbox.json"
        msg = _msg(42, sender="grok_dev", text=_distinctive_payload())

        write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=msg
        )
        entry = json.loads(inbox.read_text())[0]

        assert entry["text"] == (
            "[CAO] Message 42 ready from grok_dev. "
            "Drain: list_messages -> ack_messages"
        )

    def test_summary_equals_message_id_ready(self, tmp_path: Path) -> None:
        """summary == 'Message <id> ready' exactly (non-empty, no body)."""
        inbox = tmp_path / "inbox.json"
        msg = _msg(99, sender="worker-01", text=_distinctive_payload())

        write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=msg
        )
        entry = json.loads(inbox.read_text())[0]

        assert entry["summary"] == "Message 99 ready"
        # Non-empty so summary-keyed listings never render a blank row.
        assert entry["summary"].strip() != ""

    def test_multiline_payload_first_line_does_not_leak(self, tmp_path: Path) -> None:
        """Even a short distinctive first line does not appear in the entry.

        The pre-F654 writer used ``message.split('\\n', 1)[0]`` as the preview,
        so a multiline body's first line was the leak vector. Guard it.
        """
        inbox = tmp_path / "inbox.json"
        first_line = f"FIRSTLINE_{_DISTINCTIVE_TOKEN}"
        msg = _msg(3, sender="kiro_dev", text=f"{first_line}\nsecond line\nthird")

        write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=msg
        )
        entry = json.loads(inbox.read_text())[0]

        assert _DISTINCTIVE_TOKEN not in entry["text"]
        assert _DISTINCTIVE_TOKEN not in entry["summary"]
        assert "FIRSTLINE_" not in entry["text"]
        assert "FIRSTLINE_" not in entry["summary"]


# ===========================================================================
# AC2 — Identity preserved (byte-compare against the pinned derivation)
# ===========================================================================


class TestAC2IdentityPreserved:
    def test_msg_id_equals_callback_notification_id(self, tmp_path: Path) -> None:
        """The written msg_id equals callback_notification_id(mailbox_id, row_id)."""
        inbox = tmp_path / "inbox.json"
        msg = _msg(5, sender="kiro_dev", text=_distinctive_payload())

        write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_supervisor_main", message=msg
        )
        entry = json.loads(inbox.read_text())[0]

        assert entry["msg_id"] == callback_notification_id("mb_supervisor_main", 5)

    def test_msg_id_matches_pinned_golden_vector(self, tmp_path: Path) -> None:
        """Byte-compare against the F136 pinned golden identity.

        callback_notification_id('mb_supervisor_main', 42) is frozen forever at
        this UUID (test_f136_callback_delivery.TestAC18GoldenIdentity). The
        contentless payload change must not perturb identity by one byte.
        """
        inbox = tmp_path / "inbox.json"
        msg = _msg(42, sender="kiro_dev", text=_distinctive_payload())

        write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_supervisor_main", message=msg
        )
        entry = json.loads(inbox.read_text())[0]

        assert entry["msg_id"] == "1c4526f7-c4e9-50e7-87c3-c6e1b8674bac"

    def test_from_is_cao_bridge(self, tmp_path: Path) -> None:
        """The `from` field stays 'cao-bridge' (read-marking correlation key)."""
        inbox = tmp_path / "inbox.json"
        write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=_msg(1)
        )
        entry = json.loads(inbox.read_text())[0]
        assert entry["from"] == _TEAMMATE_FROM == "cao-bridge"

    def test_read_flag_defaults_false(self, tmp_path: Path) -> None:
        """Default read flag is False so the harness can wake on it."""
        inbox = tmp_path / "inbox.json"
        write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=_msg(1)
        )
        entry = json.loads(inbox.read_text())[0]
        assert entry["read"] is False


# ===========================================================================
# AC2b — Full schema
# ===========================================================================


class TestAC2bSchema:
    def test_entry_carries_full_expected_schema(self, tmp_path: Path) -> None:
        """Every expected field is present with the correct type/default.

        Asserted explicitly so a shape regression can't hide behind the
        content assertions.
        """
        inbox = tmp_path / "inbox.json"
        msg = _msg(11, sender="kiro_dev", text=_distinctive_payload())

        write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=msg
        )
        entry = json.loads(inbox.read_text())[0]

        assert set(entry.keys()) == {
            "type",
            "from",
            "text",
            "timestamp",
            "summary",
            "read",
            "msgV",
            "msg_id",
        }
        assert entry["type"] == "message"
        assert entry["from"] == "cao-bridge"
        assert isinstance(entry["text"], str) and entry["text"]
        assert entry["timestamp"] == _NOW.isoformat()
        assert entry["summary"] == "Message 11 ready"
        assert entry["read"] is False
        assert entry["msgV"] == 1
        assert entry["msg_id"] == callback_notification_id("mb_test", 11)


# ===========================================================================
# AC3 — Dedup + accounting unchanged
# ===========================================================================


class TestAC3DedupAccounting:
    def test_duplicate_emission_is_already_present(self, tmp_path: Path) -> None:
        """Re-emitting the same id yields already_present, one entry on disk."""
        inbox = tmp_path / "inbox.json"
        msg = _msg(8, sender="kiro_dev", text=_distinctive_payload())

        r1 = write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=msg
        )
        r2 = write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=msg
        )
        assert r1.kind == "written"
        assert r2.kind == "already_present"

        entries = json.loads(inbox.read_text())
        assert len(entries) == 1

    def test_rebuilt_entry_same_row_is_idempotent(self, tmp_path: Path) -> None:
        """F654 idempotency: a rebuilt entry for the same row has identical text
        → already_present, NOT identity_conflict (the F175 clobber case converges).
        """
        inbox = tmp_path / "inbox.json"
        # Same id + same created_at + same sender → byte-identical entry text.
        m1 = _msg(4, sender="kiro_dev", text="body one")
        m2 = _msg(4, sender="kiro_dev", text="body two — different, but not in the entry")

        r1 = write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=m1
        )
        r2 = write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=m2
        )
        assert r1.kind == "written"
        # Because the body no longer appears in text, differing bodies for the
        # same (mailbox,row,sender,timestamp) now converge to already_present.
        assert r2.kind == "already_present"
        assert len(json.loads(inbox.read_text())) == 1

    def test_mixed_version_old_format_survivor_is_identity_conflict(
        self, tmp_path: Path
    ) -> None:
        """EXPECTED-not-failure: an old-format entry surviving the redeploy has
        the same msg_id but different text → identity_conflict on re-emission.

        This models the first post-deploy drain window. It is benign: the
        surviving old entry still carries that id's wake.
        """
        inbox = tmp_path / "inbox.json"
        msg = _msg(6, sender="kiro_dev", text="hello world payload")
        old_msg_id = callback_notification_id("mb_test", 6)

        # Simulate a pre-F654 (old-format) entry already on disk for this id.
        old_entry = {
            "type": "message",
            "from": _TEAMMATE_FROM,
            "text": "[CAO:kiro_dev] hello world payload\n\n---\nMessage 6 ready. "
            "Drain: list_messages -> ack_messages",
            "timestamp": _NOW.isoformat(),
            "summary": "kiro_dev: hello world payload",
            "read": False,
            "msgV": 1,
            "msg_id": old_msg_id,
        }
        inbox.write_text(json.dumps([old_entry], indent=2), encoding="utf-8")

        r = write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=msg
        )
        # Same msg_id, different immutable text → identity_conflict. Expected.
        assert r.kind == "identity_conflict"
        # The surviving old entry is untouched (still carries the wake).
        entries = json.loads(inbox.read_text())
        assert len(entries) == 1
        assert entries[0]["msg_id"] == old_msg_id

    def test_return_kinds_unchanged_written_and_already_present(
        self, tmp_path: Path
    ) -> None:
        """Return kinds are the same closed set the accounting layer keys on.

        inbox_service.py:1249 doorbell arbitration keys on outcome.written, which
        is driven by the 'written' kind. This asserts the writer still emits
        exactly 'written' on first insert and 'already_present' on dup — so the
        guard-value inputs (written count) are byte-identical pre/post F654.
        """
        inbox = tmp_path / "inbox.json"
        first = write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=_msg(1)
        )
        dup = write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=_msg(1)
        )
        assert first.kind == "written"
        assert dup.kind == "already_present"


# ===========================================================================
# AC4 — Mutation targets (must be killable)
# ===========================================================================
#
# These tests are RED if the corresponding mutation is applied to the writer:
#   (a) restore the message preview into `text`      -> test_ac1_* red
#   (b) restore the summary derivation into `summary`-> test_ac1_summary red
#   (c) perturb the msg_id derivation                -> test_ac2_* red
#
# The assertions live in TestAC1Contentless / TestAC2IdentityPreserved above;
# this class documents the mutation ledger and adds a direct guard that a
# preview-shaped body substring cannot appear, so mutation (a) is unambiguous.


class TestAC4MutationTargets:
    def test_mutation_a_no_preview_bracket_prefix(self, tmp_path: Path) -> None:
        """Mutation (a) kill: the old '[CAO:<sender>] <preview>' shape is gone.

        Restoring the preview would reintroduce the '[CAO:<sender>] ' prefix
        followed by body text. The new format is '[CAO] Message ...'.
        """
        inbox = tmp_path / "inbox.json"
        msg = _msg(2, sender="kiro_dev", text=_distinctive_payload())
        write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=msg
        )
        entry = json.loads(inbox.read_text())[0]
        # Old prefix embedded the sender inside the bracket: '[CAO:kiro_dev]'.
        assert "[CAO:kiro_dev]" not in entry["text"]
        assert entry["text"].startswith("[CAO] Message 2 ready from kiro_dev.")

    def test_mutation_b_summary_has_no_sender_colon_body(self, tmp_path: Path) -> None:
        """Mutation (b) kill: the old 'worker: <preview>' summary shape is gone."""
        inbox = tmp_path / "inbox.json"
        msg = _msg(2, sender="kiro_dev", text=_distinctive_payload())
        write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=msg
        )
        entry = json.loads(inbox.read_text())[0]
        assert not entry["summary"].startswith("kiro_dev:")
        assert entry["summary"] == "Message 2 ready"

    def test_mutation_c_msg_id_perturbation_would_break_golden(
        self, tmp_path: Path
    ) -> None:
        """Mutation (c) kill: any perturbation of the derivation breaks the golden.

        Redundant with TestAC2IdentityPreserved.test_msg_id_matches_pinned_golden_vector,
        stated here so the mutation ledger has a named home.
        """
        inbox = tmp_path / "inbox.json"
        write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_supervisor_main", message=_msg(42)
        )
        entry = json.loads(inbox.read_text())[0]
        assert entry["msg_id"] == "1c4526f7-c4e9-50e7-87c3-c6e1b8674bac"


# ===========================================================================
# Sanity: token-size intent (AC5 is a live-box probe, not asserted here)
# ===========================================================================


class TestContentlessSizeIntent:
    def test_entry_text_is_short(self, tmp_path: Path) -> None:
        """The contentless entry is a short pointer regardless of body size."""
        inbox = tmp_path / "inbox.json"
        huge = "Z" * 10_000
        write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id="mb_test", message=_msg(1, text=huge)
        )
        entry = json.loads(inbox.read_text())[0]
        # A generous ceiling: the pointer never scales with the body.
        assert len(entry["text"]) < 120
        assert "Z" * 16 not in entry["text"]
