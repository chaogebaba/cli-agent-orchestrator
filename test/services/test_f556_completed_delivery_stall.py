"""F556: send_message to a status=completed worker never delivers.

PROVEN ROOT CAUSE (see orchestrator/tmp/orch/f556-delivery-completed-build-report.md):
A stuck non-claude DELIVERING attempt on a terminal blocks EVERY subsequent
delivery to that terminal — begin_delivery_attempt_if_no_other_delivering()
returns "delivering_conflict" and deliver_pending()'s pre-open guard
list_delivering_attempts_for_terminal() short-circuits — so no new attempt is
ever opened (matches the incident's "zero delivery attempts" trace). The only
code that settles a stuck DELIVERING row for a NON-claude provider was
recover_stale_deliveries(recurring=False), which runs ONCE at process startup.
The periodic reconciliation heartbeat calls recover_stale_deliveries(
recurring=True), whose branch recovered ONLY provider == "claude_code" attempts
(list_stale_open_claude_attempts). A kiro_cli (or any non-claude) stuck
DELIVERING row was therefore never cleared while the server stayed up, and the
pending backlog behind it stalled indefinitely — exactly what the ready-backlog
watchdog reports ("status=completed ... no open delivery attempt ...
Reconciliation remains the retry owner").

FIX: the recurring reconciliation heartbeat now runs the SAME provider-agnostic
recovery the startup sweep uses, for aged non-claude stuck DELIVERING rows,
gated by WPM2_STALE_OPEN_AGE_SECONDS so it never races an in-flight paste.
Clearing the stuck row lifts the delivering_conflict exclusion and the backlog
delivers.

These tests drive DEPLOYED entry points against the real_sqlite_env fixture.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType


def _seed_terminal(session, term_id: str, provider: str = "kiro_cli"):
    from cli_agent_orchestrator.clients.database import TerminalModel

    session.add(
        TerminalModel(
            id=term_id,
            tmux_session="sess",
            tmux_window=f"win-{term_id}",
            provider=provider,
            agent_profile="developer",
            lifecycle="sticky",
            init_state="ready",
            lifecycle_generation=1,
            metadata_json=json.dumps({}),
        )
    )


def _seed_stuck_delivering(
    TestSession,
    *,
    receiver: str,
    provider: str,
    age_seconds: int,
    sender: str = "supervis",
    body: str = "prior task brief",
):
    """Seed one message stuck in DELIVERING plus its unsettled attempt row.

    Mirrors the shape a real delivery leaves behind when the confirmation never
    settled: the inbox row is DELIVERING and the newest attempt has settled_at
    NULL.
    """
    from cli_agent_orchestrator.clients.database import (
        InboxDeliveryAttemptMemberModel,
        InboxDeliveryAttemptModel,
        InboxModel,
    )

    started = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    with TestSession() as s:
        _seed_terminal(s, sender)
        _seed_terminal(s, receiver, provider=provider)
        stuck = InboxModel(
            sender_id=sender,
            receiver_id=receiver,
            logical_receiver_id=None,
            message=body,
            orchestration_type="send_message",
            status=MessageStatus.DELIVERING.value,
            enqueue_generation=1,
            created_at=started,
        )
        s.add(stuck)
        s.flush()
        attempt_uuid = f"stuck-{receiver}"
        s.add(
            InboxDeliveryAttemptModel(
                attempt_uuid=attempt_uuid,
                receiver_terminal_id=receiver,
                provider=provider,
                started_at=started,
                last_at=started,
                settled_at=None,  # never confirmed -> stuck DELIVERING
                payload_hash="deadbeef",
                payload_length=len(body),
                sender_id=sender,
                orchestration_type="send_message",
                evidence="{}",
            )
        )
        s.add(
            InboxDeliveryAttemptMemberModel(
                attempt_uuid=attempt_uuid, message_id=stuck.id, position=0
            )
        )
        s.commit()
        return stuck.id, attempt_uuid


def _seed_fresh_pending(
    TestSession, *, receiver: str, sender: str = "supervis", body="ACK proceed"
):
    from cli_agent_orchestrator.clients.database import InboxModel

    with TestSession() as s:
        m = InboxModel(
            sender_id=sender,
            receiver_id=receiver,
            logical_receiver_id=None,
            message=body,
            orchestration_type="send_message",
            status=MessageStatus.PENDING.value,
            enqueue_generation=1,
            created_at=datetime.now(timezone.utc),
        )
        s.add(m)
        s.commit()
        return m.id


def _attempt_status(TestSession, attempt_uuid: str):
    from cli_agent_orchestrator.clients.database import InboxDeliveryAttemptModel

    with TestSession() as s:
        row = s.get(InboxDeliveryAttemptModel, attempt_uuid)
        return None if row is None else (row.settled_at, row.outcome)


def _msg_status(TestSession, message_id: int) -> str:
    from cli_agent_orchestrator.clients.database import InboxModel

    with TestSession() as s:
        return s.get(InboxModel, message_id).status


@pytest.mark.xdist_group("real_sqlite")
class TestF556CompletedDeliveryStall:
    def test_stuck_delivering_blocks_new_attempt(self, real_sqlite_env):
        """PART A (the block): a fresh PENDING row for a terminal that already
        has a stuck DELIVERING row cannot open a delivery attempt — the opener
        returns 'delivering_conflict'. This is why the trace shows zero attempts.

        Mutant: make begin_delivery_attempt_if_no_other_delivering ignore the
        open DELIVERING authority -> opener returns 'opened' and this fails.
        """
        TestSession = real_sqlite_env["TestSession"]
        _seed_stuck_delivering(
            TestSession, receiver="worker01", provider="kiro_cli", age_seconds=4000
        )
        fresh_id = _seed_fresh_pending(TestSession, receiver="worker01")

        from cli_agent_orchestrator.clients.database import (
            begin_delivery_attempt_if_no_other_delivering,
            get_pending_messages,
            list_delivering_attempts_for_terminal,
        )

        # The pre-open guard deliver_pending consults sees the stuck authority.
        assert list_delivering_attempts_for_terminal(
            "worker01"
        ), "stuck DELIVERING row must present as open delivery authority"

        fresh = [m for m in get_pending_messages("worker01") if m.id == fresh_id]
        assert fresh, "fresh row must be pending and selectable"

        opened = begin_delivery_attempt_if_no_other_delivering(
            fresh,
            "worker01",
            "kiro_cli",
            "freshhash",
            payload_length=len(fresh[0].message),
        )
        assert opened.kind == "delivering_conflict", opened.kind
        # No attempt opened -> the fresh row stays PENDING (never DELIVERING).
        assert _msg_status(TestSession, fresh_id) == MessageStatus.PENDING.value

    def test_recurring_reconcile_recovers_non_claude_stuck_delivering(self, real_sqlite_env):
        """PART B (the fix): the periodic reconciliation heartbeat
        (recover_stale_deliveries(recurring=True)) now settles an aged stuck
        non-claude (kiro_cli) DELIVERING attempt via the SAME provider-agnostic
        recovery the startup sweep uses. Clearing the DELIVERING row lifts the
        'delivering_conflict' exclusion so the pending backlog behind it can
        finally be delivered.

        Pre-fix, the recurring branch only ran the claude-only selector
        (list_stale_open_claude_attempts), so this kiro attempt stayed unsettled
        forever and PART A's block persisted.

        Mutant: revert the recurring branch to claude-only (drop the
        list_stale_delivering_messages sweep) -> the attempt stays unsettled and
        this test fails.
        """
        TestSession = real_sqlite_env["TestSession"]
        stuck_msg, kiro_attempt = _seed_stuck_delivering(
            TestSession, receiver="worker01", provider="kiro_cli", age_seconds=4000
        )

        from cli_agent_orchestrator.clients.database import (
            list_stale_open_claude_attempts,
        )
        from cli_agent_orchestrator.services.inbox_service import (
            WPM2_STALE_OPEN_AGE_SECONDS,
            InboxService,
        )

        # The claude-only selector never sees the kiro attempt: the recurring
        # branch cannot rely on it alone (this is the gap the fix closes).
        selected = {
            a["attempt_uuid"] for a in list_stale_open_claude_attempts(WPM2_STALE_OPEN_AGE_SECONDS)
        }
        assert kiro_attempt not in selected

        assert _attempt_status(TestSession, kiro_attempt) == (None, None)
        InboxService().recover_stale_deliveries(recurring=True)
        settled_at, outcome = _attempt_status(TestSession, kiro_attempt)

        # FIX: the heartbeat settled the stuck kiro attempt (here 'interrupted'
        # because the fake pane has no resolvable transcript -> row returns to
        # PENDING), lifting the exclusion.
        assert settled_at is not None, "recurring sweep must settle the stuck kiro attempt"
        assert _msg_status(TestSession, stuck_msg) == MessageStatus.PENDING.value

    def test_recurring_reconcile_leaves_fresh_delivering_untouched(self, real_sqlite_env):
        """AGE GATE: a stuck-looking row whose newest attempt is YOUNGER than
        WPM2_STALE_OPEN_AGE_SECONDS is a legitimately in-flight paste — the
        recurring heartbeat must NOT adopt it (that would race a healthy
        deliver_pending mid-confirmation).

        Mutant: drop the min_age_seconds gate on list_stale_delivering_messages
        in the recurring branch -> the fresh attempt is settled and this fails.
        """
        TestSession = real_sqlite_env["TestSession"]
        stuck_msg, fresh_attempt = _seed_stuck_delivering(
            TestSession, receiver="worker01", provider="kiro_cli", age_seconds=1
        )

        from cli_agent_orchestrator.services.inbox_service import InboxService

        InboxService().recover_stale_deliveries(recurring=True)

        # Younger than the age gate -> untouched; still DELIVERING, attempt open.
        assert _attempt_status(TestSession, fresh_attempt) == (None, None)
        assert _msg_status(TestSession, stuck_msg) == MessageStatus.DELIVERING.value
