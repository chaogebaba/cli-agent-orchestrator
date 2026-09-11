"""F476 r3 (#388): the single-wake CURSOR, observed without its transports.

**What this file is.** ``test_f476_r3_bypass_closure.py`` proved "one wake per
id" by counting emissions on two transports — the WS advisory frame and the
F461-coalesced native ring. WP-ARCH 3c deletes both, and the r1 review's S2 is
that deleting the file with them drops coverage of machinery that SURVIVED:
``claim_unnotified_wake`` and ``commit_wake`` still gate
``_f136_run_callback_delivery``. So the six arms whose subject is the CURSOR are
re-pointed here, and the two that were genuinely about transport arbitration
(``test_ws_fires_native_suppressed``,
``test_ws_armed_but_send_fails_falls_back_to_native``) are correctly gone.

**The observation seam changed; the property did not.** A claim that emits
nothing is still a claim: the runner reports ``written`` and the mailbox row
carries ``callback_notified_through_id``. Those two are what the cursor actually
controls, and they are what these arms count. Counting a transport was always one
level removed from the invariant — the deleted file could not have distinguished
"the cursor held the row back" from "the doorbell happened not to ring".

**A third observable was dropped a slice later, not weakened.** This file was
written against the K2 CONTENT channel as well: each arm read the JSON inbox at
``cc_inbox_path`` and asserted how many entries the carrier had appended. WP-ARCH
3c K2 deletes that writer with ``teammate_push_service``; ``written`` is now
purely the cursor's count of claimed rows and ``_f136_post_delivery`` emits
nothing at all. Re-pointing those assertions at the file would have been
asserting an empty list against an empty list in every direction, which is the
vacuity this file's opening note exists to refuse — so the file leg is removed
and the cursor column carries the paired direction alone. ``cc_inbox_path`` stays
in the seed because the mailbox row still has the column and a claim must be
shown not to depend on it.

The #388 sample is preserved verbatim in the replay arm: the bridge replayed
seven already-acked ids in one batch at 07:47Z.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as dbmod
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
)
from cli_agent_orchestrator.services.mailbox_service import ack_messages

_NOW = datetime(2026, 8, 30, 7, 47, 0, tzinfo=timezone.utc)


@pytest.fixture
def r3_env(tmp_path: Any, monkeypatch: Any) -> Any:
    eng = create_engine(
        f"sqlite:///{tmp_path / 'f476r3.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)  # includes wake_notified_at/_streak/_id columns
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    monkeypatch.setattr(dbmod, "SessionLocal", sessions)
    monkeypatch.setattr(dbmod, "engine", eng)
    monkeypatch.setattr("cli_agent_orchestrator.services.mailbox_service.SessionLocal", sessions)
    return {"sessions": sessions, "engine": eng, "tmp_path": tmp_path}


def _seed(env: Any, *, cursor: int = 0, consumed: int = 0, path: str | None = None) -> str:
    sessions = env["sessions"]
    inbox_path = path or str(env["tmp_path"] / "cc-inbox.json")
    with sessions.begin() as db:
        db.add(
            TerminalModel(
                id="t1",
                tmux_session="test",
                tmux_window="t1",
                provider="claude_code",
                agent_profile="supervisor",
                lifecycle_generation=1,
            )
        )
        db.add(
            MailboxModel(
                id="mb_sup",
                session_name="test",
                role="supervisor",
                current_terminal_id="t1",
                generation=1,
                consumed_through_id=consumed,
                callback_notified_through_id=cursor,
                cc_inbox_path=inbox_path,
                cc_inbox_path_version=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        db.flush()
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_sup",
                generation=1,
                terminal_id="t1",
                published_at=_NOW,
            )
        )
    return inbox_path


def _row(env: Any, row_id: int, *, status: str = "pending") -> None:
    with env["sessions"].begin() as db:
        db.add(
            InboxModel(
                id=row_id,
                sender_id="worker-1",
                receiver_id="t1",
                logical_receiver_id="mb_sup",
                message=f"msg-{row_id}",
                orchestration_type="send_message",
                status=status,
                enqueue_generation=1,
                created_at=_NOW,
            )
        )


def _run(env: Any) -> Any:
    """One production runner pass, plus the post-delivery step.

    Both halves are still driven even though the post-delivery step no longer
    rings anything: it is where the re-arm bookkeeping lives, and a test that
    stopped calling it would be observing a shorter path than production runs.
    """
    from unittest.mock import MagicMock

    from cli_agent_orchestrator.services.inbox_service import inbox_service

    mock_loop = MagicMock()
    mock_loop.is_closed.return_value = False
    old_loop = inbox_service._delivery_loop
    inbox_service._delivery_loop = mock_loop
    try:
        outcome = inbox_service._f136_run_callback_delivery("t1")
        inbox_service._f136_post_delivery("t1", outcome)
    finally:
        inbox_service._delivery_loop = old_loop
    return outcome


def _cursor(env: Any) -> int:
    with env["sessions"]() as db:
        mb = db.query(MailboxModel).filter_by(id="mb_sup").one()
        return int(mb.callback_notified_through_id or 0)


class TestOneWakePerId:
    def test_single_wake_and_cursor_advance(self, r3_env: Any) -> None:
        """The baseline: one claimable row is written once and moves the cursor."""
        _seed(r3_env, cursor=0)
        _row(r3_env, 1)

        outcome = _run(r3_env)

        assert outcome.written == 1
        assert _cursor(r3_env) == 1

    def test_second_run_same_row_no_reemit(self, r3_env: Any) -> None:
        """A rewake poll of the same still-pending row must NOT re-emit.

        The cursor already covers it, so the claim comes back empty. This is the
        arm that makes the cursor observable at all: without the advance, the
        second pass would write the row a second time.
        """
        _seed(r3_env, cursor=0)
        _row(r3_env, 1)

        first = _run(r3_env)
        assert first.written == 1

        second = _run(r3_env)
        assert second.written == 0, "the same row was claimed twice"
        assert _cursor(r3_env) == 1, "the cursor moved a second time for one row"


class TestAckedIdNeverReemitted:
    def test_ack_then_rerun_zero_emits(self, r3_env: Any) -> None:
        _seed(r3_env, cursor=0)
        _row(r3_env, 1)

        first = _run(r3_env)
        assert first.written == 1

        # The seat drains and acks the id (the authoritative digest path).
        ack_messages("t1", up_to_id=1)

        second = _run(r3_env)
        assert second.written == 0
        assert _cursor(r3_env) == 1

    def test_cursor_already_past_reconnect_no_emit(self, r3_env: Any) -> None:
        """Reconnect: the row was acked before this incarnation ever polls.

        A fresh runner pass emits nothing — an acked id never re-surfaces on
        reconnect, which is the half of #388 a restart could otherwise undo.
        """
        _seed(r3_env, cursor=0, consumed=5)
        _row(r3_env, 3)  # id at/below the consumed (acked) cursor

        outcome = _run(r3_env)

        assert outcome.written == 0
        assert _cursor(r3_env) == 0, "an acked id moved the wake cursor on reconnect"

    def test_bridge_replay_of_acked_ids_zero_emits(self, r3_env: Any) -> None:
        """#388, the 07:47Z sample: the bridge replayed 7 acked ids in one batch.

        After the batch is acked, no runner pass may carry any of them again —
        three redundant replay attempts, as observed.
        """
        acked_ids = [2726, 2727, 2730, 2733, 2734, 2735, 2736]
        _seed(r3_env, cursor=0)
        for rid in acked_ids:
            _row(r3_env, rid)

        # Drained to quiescence rather than in ONE pass: how many rows a single
        # claim takes is a batching decision, and an arm that pinned it would
        # fail on a bound change while saying nothing about the cursor. What the
        # cursor owns is that the drain TERMINATES and does not restart.
        written = 0
        for _ in range(10):
            out = _run(r3_env)
            if out.written == 0:
                break
            written += out.written
        assert written == len(acked_ids), written

        ack_messages("t1", up_to_id=max(acked_ids))
        settled = _cursor(r3_env)
        assert settled == max(acked_ids)

        for _ in range(3):  # three redundant replay attempts, as observed
            out = _run(r3_env)
            assert out.written == 0
        assert _cursor(r3_env) == settled, "a replayed acked id was claimed again"


class TestMutantLedger:
    def test_mutant_drop_cursor_advance_reemits_is_red(self, r3_env: Any, monkeypatch: Any) -> None:
        """MUTANT: drop the ``commit_wake`` cursor advance.

        ``commit_wake`` is replaced with a no-op that reports success without
        moving ``callback_notified_through_id``. The kill is the OBSERVABLE
        difference in that column: under the mutant it stays at 0, where the real
        code advances it to the emitted row id (the paired baseline below).

        **The second pass is deliberately not the assertion here.** The claim
        LEASE, not the cursor, is what makes an immediate re-poll return
        ``lease_held``, so a mutant that drops the cursor advance is invisible
        within the lease window and this arm would prove the wrong mechanism.
        What the dropped advance actually costs is silence AFTER the lease
        expires, and ``test_second_run_same_row_no_reemit`` is the arm that owns
        the paired direction.
        """
        import cli_agent_orchestrator.clients.database as dbmod_local

        _seed(r3_env, cursor=0)
        _row(r3_env, 1)

        def _mutant_commit(*_a: Any, **_kw: Any) -> Any:
            class _R:
                kind = "committed"
                reason = ""

            return _R()

        monkeypatch.setattr(dbmod_local, "commit_wake", _mutant_commit)
        first = _run(r3_env)
        assert first.written == 1
        assert _cursor(r3_env) == 0, (
            "under the mutant the cursor must NOT advance; the real commit_wake "
            "advances it to 1 (asserted in the baseline below)"
        )

    def test_real_commit_advances_cursor_mutant_baseline(self, r3_env: Any) -> None:
        """The baseline half: the REAL ``commit_wake`` advances the cursor."""
        _seed(r3_env, cursor=0)
        _row(r3_env, 1)

        out = _run(r3_env)

        assert out.written == 1
        assert _cursor(r3_env) == 1, "real commit_wake must advance the wake cursor"
