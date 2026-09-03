"""F747 (#604) — periodic idle-seat wake reconcile.

Regression cover for the 2026-09-03 sample: seat ``5561a7d1`` / mailbox
``mb_d176ebe0`` woke for inbox id 4149, then the client-side rewake watcher
died mid-idle and ids 4153-4162 sat unsurfaced for 35 minutes because nothing
re-armed it. The reconcile is the server-side net for exactly that gap.

These run against a real sqlite database (``real_sqlite_env``) rather than a
query-shape fake, so the cursor and grace-window filters are genuinely
exercised: a mutant that drops the dedup predicate has to actually fail.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cli_agent_orchestrator.services import seat_wake_reconcile
from cli_agent_orchestrator.services.seat_wake_reconcile import (
    reconcile_seat_wakes,
)

_NOW = datetime(2026, 9, 3, 8, 0, 0, tzinfo=timezone.utc)
#: Comfortably past the 90 s default grace window.
_OLD = _NOW - timedelta(seconds=600)

SEAT = "5561a7d1"
MAILBOX = "mb_d176eb"


class _RecordingPush:
    """Stand-in for ``attempt_teammate_push_reported``.

    Records one call per emission so a test can assert "at most one wake per
    batch" directly rather than inferring it.
    """

    def __init__(self, pushed: bool = True, reason: str = "pushed") -> None:
        self.pushed = pushed
        self.reason = reason
        self.calls: list[tuple[str, tuple[int, ...]]] = []

    def __call__(self, terminal_id, messages, *, mailbox_id=""):
        from cli_agent_orchestrator.services.teammate_push_service import PushOutcome

        ids = tuple(m.id for m in messages)
        self.calls.append((terminal_id, ids))
        return PushOutcome(
            pushed=self.pushed,
            reason=self.reason,
            message_ids=ids,
        )


@pytest.fixture()
def push(monkeypatch):
    recorder = _RecordingPush()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.teammate_push_service." "attempt_teammate_push_reported",
        recorder,
    )
    return recorder


def _seed(
    env,
    *,
    pending_ids: list[int],
    consumed_through_id: int = 0,
    wake_notified_id: int = 0,
    created_at: datetime | None = None,
    terminal_id: str = SEAT,
    with_terminal: bool = True,
    role: str = "supervisor",
) -> None:
    """Seed one supervisor mailbox, its terminal, and its pending inbox rows."""
    from cli_agent_orchestrator.clients.database import (
        InboxModel,
        MailboxModel,
        TerminalModel,
    )

    created = created_at or _OLD
    with env["TestSession"]() as db:
        if with_terminal:
            db.add(
                TerminalModel(
                    id=terminal_id,
                    tmux_session="cao-claude-orch5",
                    tmux_window=terminal_id,
                    provider="claude_code",
                    agent_profile="chao_supervisor",
                )
            )
        db.add(
            MailboxModel(
                id=MAILBOX,
                session_name="cao-claude-orch5",
                role=role,
                current_terminal_id=terminal_id if with_terminal else None,
                generation=32,
                consumed_through_id=consumed_through_id,
                wake_notified_id=wake_notified_id,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        for row_id in pending_ids:
            db.add(
                InboxModel(
                    id=row_id,
                    sender_id="d5286435",
                    receiver_id=terminal_id,
                    logical_receiver_id=MAILBOX,
                    message=f"callback {row_id}",
                    orchestration_type="send_message",
                    status="pending",
                    created_at=created,
                )
            )
        db.commit()


def _wake_cursor(env) -> int:
    from cli_agent_orchestrator.clients.database import MailboxModel

    with env["TestSession"]() as db:
        mailbox = db.query(MailboxModel).filter_by(id=MAILBOX).one()
        return int(mailbox.wake_notified_id)


class TestBatchToOneWake:
    def test_batch_of_nine_emits_exactly_one_wake(self, real_sqlite_env, push):
        """Nine callbacks in one batch produce one wake, not nine."""
        _seed(real_sqlite_env, pending_ids=list(range(4154, 4163)))

        decisions = reconcile_seat_wakes(now=_NOW)

        assert len(push.calls) == 1
        terminal_id, ids = push.calls[0]
        assert terminal_id == SEAT
        assert ids == tuple(range(4154, 4163))
        assert [d.outcome for d in decisions] == ["woken"]
        assert decisions[0].max_id == 4162
        assert decisions[0].terminal_id == SEAT

    def test_wake_advances_the_cursor(self, real_sqlite_env, push):
        _seed(real_sqlite_env, pending_ids=[4154, 4162])

        reconcile_seat_wakes(now=_NOW)

        assert _wake_cursor(real_sqlite_env) == 4162

    def test_wake_does_not_stamp_the_claim_lease(self, real_sqlite_env, push):
        """``wake_notified_at`` is F476's 300 s lease, not ours to take.

        Stamping it would make ``claim_unnotified_wake`` answer the seat we
        just woke with ``lease_held`` and no rows -- woken with nothing to
        drain, the failure this reconcile exists to prevent.
        """
        from cli_agent_orchestrator.clients.database import MailboxModel

        _seed(real_sqlite_env, pending_ids=[4154, 4162])

        reconcile_seat_wakes(now=_NOW)

        with real_sqlite_env["TestSession"]() as db:
            mailbox = db.query(MailboxModel).filter_by(id=MAILBOX).one()
            assert mailbox.wake_notified_at is None
            assert int(mailbox.wake_notified_id) == 4162


class TestDedup:
    def test_already_notified_ids_never_wake_again(self, real_sqlite_env, push):
        """The #568 failure inverted: an announced id must stay silent."""
        _seed(
            real_sqlite_env,
            pending_ids=[4154, 4162],
            wake_notified_id=4162,
        )

        decisions = reconcile_seat_wakes(now=_NOW)

        assert push.calls == []
        assert [d.outcome for d in decisions] == ["already_notified"]

    def test_consumed_rows_never_wake(self, real_sqlite_env, push):
        """Rows the seat already drained are below the consumption cursor."""
        _seed(
            real_sqlite_env,
            pending_ids=[4154, 4162],
            consumed_through_id=4162,
        )

        decisions = reconcile_seat_wakes(now=_NOW)

        assert push.calls == []
        assert [d.outcome for d in decisions] == ["no_pending"]

    def test_repeat_tick_with_no_new_rows_is_silent(self, real_sqlite_env, push):
        """Second tick over the same backlog emits nothing."""
        _seed(real_sqlite_env, pending_ids=list(range(4154, 4163)))

        first = reconcile_seat_wakes(now=_NOW)
        second = reconcile_seat_wakes(now=_NOW + timedelta(seconds=60))
        third = reconcile_seat_wakes(now=_NOW + timedelta(seconds=120))

        assert len(push.calls) == 1
        assert [d.outcome for d in first] == ["woken"]
        assert [d.outcome for d in second] == ["already_notified"]
        assert [d.outcome for d in third] == ["already_notified"]

    def test_only_unannounced_ids_ride_the_second_wake(self, real_sqlite_env, push):
        """A later arrival wakes once, naming only the ids not yet announced."""
        _seed(real_sqlite_env, pending_ids=[4154, 4155])

        reconcile_seat_wakes(now=_NOW)
        _add_pending(real_sqlite_env, [4160, 4161])
        reconcile_seat_wakes(now=_NOW + timedelta(seconds=60))

        assert [ids for _, ids in push.calls] == [(4154, 4155), (4160, 4161)]
        assert _wake_cursor(real_sqlite_env) == 4161


def _add_pending(env, row_ids: list[int], created_at: datetime | None = None) -> None:
    from cli_agent_orchestrator.clients.database import InboxModel

    with env["TestSession"]() as db:
        for row_id in row_ids:
            db.add(
                InboxModel(
                    id=row_id,
                    sender_id="05cf98a9",
                    receiver_id=SEAT,
                    logical_receiver_id=MAILBOX,
                    message=f"callback {row_id}",
                    orchestration_type="send_message",
                    status="pending",
                    created_at=created_at or _OLD,
                )
            )
        db.commit()


class TestRegressionSample:
    """The 4149-then-4154 sequence from the #604 sample."""

    def test_wake_for_4149_does_not_suppress_the_4154_batch(self, real_sqlite_env, push):
        # State the seat was left in: 4149 was announced by the client-side
        # watcher and drained, so both cursors sit at 4149. The watcher then
        # died and could not be re-armed while the seat stayed idle.
        _seed(
            real_sqlite_env,
            pending_ids=[],
            consumed_through_id=4149,
            wake_notified_id=4149,
        )

        # The nine callbacks that were lost, at their real ids.
        _add_pending(real_sqlite_env, list(range(4153, 4163)))

        decisions = reconcile_seat_wakes(now=_NOW)

        assert len(push.calls) == 1
        _, ids = push.calls[0]
        assert min(ids) == 4153 and max(ids) == 4162
        assert [d.outcome for d in decisions] == ["woken"]
        assert _wake_cursor(real_sqlite_env) == 4162

    def test_reconcile_fires_without_the_flags_that_were_off(
        self, real_sqlite_env, push, monkeypatch
    ):
        """The three server-side gates were false in the sample deployment.

        ``supervisor.mailbox_pull``, ``supervisor.teammate_push`` and
        ``supervisor.wake.native`` were all off, which is why the existing
        pull-mode reconciler returned at its first gate. The F747 net must not
        inherit that dependency.
        """
        from cli_agent_orchestrator.services.config_service import ConfigService

        real_get = ConfigService.get

        def _fake_get(path, default=None, override=None):
            if path in {
                "supervisor.mailbox_pull",
                "supervisor.teammate_push",
                "supervisor.wake.native",
                "supervisor.wake.ws_monitor",
            }:
                return False
            return real_get(path, default, override)

        monkeypatch.setattr(ConfigService, "get", staticmethod(_fake_get))
        _seed(real_sqlite_env, pending_ids=list(range(4153, 4163)))

        decisions = reconcile_seat_wakes(now=_NOW)

        assert len(push.calls) == 1
        assert [d.outcome for d in decisions] == ["woken"]


class TestGating:
    def test_disabled_switch_emits_nothing(self, real_sqlite_env, push, monkeypatch):
        monkeypatch.setattr(seat_wake_reconcile, "_enabled", lambda: False)
        _seed(real_sqlite_env, pending_ids=[4154, 4162])

        assert reconcile_seat_wakes(now=_NOW) == []
        assert push.calls == []

    def test_switch_defaults_on(self):
        assert seat_wake_reconcile._enabled() is True

    def test_rows_inside_the_grace_window_are_left_to_the_fast_paths(self, real_sqlite_env, push):
        _seed(
            real_sqlite_env,
            pending_ids=[4154],
            created_at=_NOW - timedelta(seconds=5),
        )

        decisions = reconcile_seat_wakes(now=_NOW)

        assert push.calls == []
        assert [d.outcome for d in decisions] == ["no_pending"]

    def test_mailbox_without_a_terminal_is_skipped(self, real_sqlite_env, push):
        _seed(real_sqlite_env, pending_ids=[4154], with_terminal=False)

        decisions = reconcile_seat_wakes(now=_NOW)

        assert push.calls == []
        assert [d.outcome for d in decisions] == ["no_terminal"]

    def test_non_supervisor_mailboxes_are_out_of_scope(self, real_sqlite_env, push):
        _seed(real_sqlite_env, pending_ids=[4154], role="worker")

        assert reconcile_seat_wakes(now=_NOW) == []
        assert push.calls == []


class TestFailureHandling:
    def test_failed_push_leaves_the_cursor_for_the_next_tick(self, real_sqlite_env, monkeypatch):
        failing = _RecordingPush(pushed=False, reason="no_inbox_path")
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service."
            "attempt_teammate_push_reported",
            failing,
        )
        _seed(real_sqlite_env, pending_ids=[4154, 4162])

        decisions = reconcile_seat_wakes(now=_NOW)

        assert [d.outcome for d in decisions] == ["push_failed"]
        assert decisions[0].reason == "no_inbox_path"
        # Not advanced: the next tick must retry the same batch.
        assert _wake_cursor(real_sqlite_env) == 0

    def test_one_mailbox_fault_does_not_abort_the_sweep(self, real_sqlite_env, push, monkeypatch):
        _seed(real_sqlite_env, pending_ids=[4154])

        calls: list[str] = []
        real_one = seat_wake_reconcile._reconcile_one

        def _boom(mailbox, cutoff):
            calls.append(mailbox.id)
            raise RuntimeError("simulated mailbox fault")

        monkeypatch.setattr(seat_wake_reconcile, "_reconcile_one", _boom)

        assert reconcile_seat_wakes(now=_NOW) == []
        assert calls == [MAILBOX]
        assert real_one is not None


class TestLogging:
    def test_wake_decision_is_logged_at_info_with_the_seat_id(self, real_sqlite_env, push, caplog):
        _seed(real_sqlite_env, pending_ids=list(range(4154, 4163)))

        with caplog.at_level("INFO", logger="cli_agent_orchestrator.services.seat_wake_reconcile"):
            reconcile_seat_wakes(now=_NOW)

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "f747_seat_wake_reconcile" in m and SEAT in m and "outcome=woken" in m for m in messages
        ), messages

    def test_already_notified_is_also_logged_at_info(self, real_sqlite_env, push, caplog):
        _seed(real_sqlite_env, pending_ids=[4162], wake_notified_id=4162)

        with caplog.at_level("INFO", logger="cli_agent_orchestrator.services.seat_wake_reconcile"):
            reconcile_seat_wakes(now=_NOW)

        assert any(
            "outcome=already_notified" in r.getMessage() and SEAT in r.getMessage()
            for r in caplog.records
        )

    def test_steady_state_ticks_do_not_spam_info(self, real_sqlite_env, push, caplog):
        """A quiet mailbox must not write an INFO line every minute forever."""
        _seed(real_sqlite_env, pending_ids=[])

        with caplog.at_level("INFO", logger="cli_agent_orchestrator.services.seat_wake_reconcile"):
            reconcile_seat_wakes(now=_NOW)

        assert caplog.records == []


class TestIntervalClamp:
    def test_interval_defaults_and_rejects_a_spin(self, monkeypatch):
        from cli_agent_orchestrator.services.config_service import ConfigService

        assert seat_wake_reconcile._interval_s() == 60.0

        monkeypatch.setattr(
            ConfigService,
            "get",
            staticmethod(lambda path, default=None, override=None: 0.0),
        )
        assert seat_wake_reconcile._interval_s() == 60.0
