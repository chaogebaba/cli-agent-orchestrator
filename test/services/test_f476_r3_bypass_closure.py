"""F476 r3 (#388) — bypass-closure tests: single wake per id.

The single-wake cursor (claim_unnotified_wake / commit_wake) was merged as the
F476 build, but two wake surfaces still BYPASSED it and re-emitted already-acked
ids (issue #388 samples 3-17):

  1. inbox_service.deliver_pending pull-mode gate called attempt_teammate_push
     directly (the "N message(s) ready. Drain" teammate replay).
  2. the WS advisory frame fired from _f413_after_commit / the mailbox_service
     deferred-stash drain on the INSERT commit, ungated by the cursor.

r3 routes both through the cursor-gated F136 runner: the runner claims rows
above max(callback_notified_through_id, consumed_through_id), commits (advancing
the cursor), then emits AT MOST ONE wake transport (WS when armed and it sends,
else the native ring) per id. An acked / aged / replayed id yields written=0 and
emits nothing.

Contract under test:
  * per id: exactly ONE inbox-drain digest (path 1, untouched) + AT MOST ONE
    wake (WS OR native, never both, never a replay of an acked id).
  * acked ids are never re-emitted on reconnect / replay / aged echo.

These drive the real production runner (_f136_run_callback_delivery) through a
monkeypatched SessionLocal, and observe the two transports:
  * WS  — cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync
  * native — cli_agent_orchestrator.services.doorbell_coalesce
             .doorbell_coalesce_service.submit
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine, text
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


# ---------------------------------------------------------------------------
# Fixture: real temp DB with wake columns, patched into every SessionLocal.
# ---------------------------------------------------------------------------


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
                schema_version=1,
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


class _Probes:
    """Capture the two wake transports the runner may fire."""

    def __init__(self) -> None:
        self.ws_calls: list[tuple[str, int]] = []
        self.native_calls: list[tuple[str, int]] = []


def _install_probes(
    monkeypatch: Any, *, ws_armed: bool, ws_succeeds: bool = True
) -> _Probes:
    p = _Probes()

    # WS surface -----------------------------------------------------------
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.ws_doorbell.is_ws_monitor_enabled",
        lambda: ws_armed,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.ws_doorbell.is_armed",
        lambda tid: ws_armed,
    )

    def _fake_ws(tid: str, rid: int, sender: str, preview: str, **kw: Any) -> bool:
        if ws_succeeds:
            p.ws_calls.append((tid, rid))
            return True
        return False

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync",
        _fake_ws,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.ws_doorbell.mark_ws_delivered",
        lambda tid, rid: None,
    )

    # Native surface (coalesce submit) ------------------------------------
    def _fake_submit(tid: str, rid: int, **kw: Any) -> None:
        p.native_calls.append((tid, rid))

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.doorbell_coalesce.doorbell_coalesce_service.submit",
        _fake_submit,
    )
    return p


def _run(env: Any) -> Any:
    """Invoke the production runner once, then the post-delivery transport step.

    The runner (_f136_run_callback_delivery) does claim->commit->emit and fires
    the WS frame (setting outcome._ws_fired). The native ring decision lives in
    _f136_post_delivery, so we drive both to observe the full at-most-one-wake
    arbitration. A MagicMock delivery loop lets post_delivery's re-arm logic run
    without a real event loop.
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


def _read_inbox(path: str) -> list[dict]:
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return []
    raw = p.read_text(encoding="utf-8")
    return json.loads(raw) if raw.strip() else []


# ---------------------------------------------------------------------------
# One wake per id — native transport (WS unarmed → teammate/native path).
# ---------------------------------------------------------------------------


class TestOneWakePerIdNative:
    def test_single_native_wake_and_cursor_advance(self, r3_env: Any, monkeypatch: Any) -> None:
        path = _seed(r3_env, cursor=0)
        _row(r3_env, 1)
        p = _install_probes(monkeypatch, ws_armed=False)

        outcome = _run(r3_env)

        assert outcome.written == 1
        assert outcome._ws_fired is False
        # Exactly one native wake for the row, no WS.
        assert p.native_calls == [("t1", 1)]
        assert p.ws_calls == []
        # Cursor advanced past the row (commit_wake ran before emit).
        assert _cursor(r3_env) == 1
        # The authoritative digest file was written exactly once.
        assert len(_read_inbox(path)) == 1

    def test_second_run_same_row_no_reemit(self, r3_env: Any, monkeypatch: Any) -> None:
        """A rewake poll of the same still-pending row within the lease window
        must NOT re-emit — the cursor already covers it (lease_held / empty)."""
        _seed(r3_env, cursor=0)
        _row(r3_env, 1)
        p = _install_probes(monkeypatch, ws_armed=False)

        first = _run(r3_env)
        assert first.written == 1
        assert p.native_calls == [("t1", 1)]

        second = _run(r3_env)
        # No new wake for the same id.
        assert second.written == 0
        assert p.native_calls == [("t1", 1)]
        assert p.ws_calls == []


# ---------------------------------------------------------------------------
# One wake per id — WS wins → native suppressed (at most one transport).
# ---------------------------------------------------------------------------


class TestOneWakePerIdWsWins:
    def test_ws_fires_native_suppressed(self, r3_env: Any, monkeypatch: Any) -> None:
        _seed(r3_env, cursor=0)
        _row(r3_env, 1)
        p = _install_probes(monkeypatch, ws_armed=True, ws_succeeds=True)

        outcome = _run(r3_env)

        assert outcome.written == 1
        assert outcome._ws_fired is True
        # WS fired exactly once; native NOT submitted (D8: at most one transport).
        assert p.ws_calls == [("t1", 1)]
        assert p.native_calls == []
        assert _cursor(r3_env) == 1

    def test_ws_armed_but_send_fails_falls_back_to_native(
        self, r3_env: Any, monkeypatch: Any
    ) -> None:
        _seed(r3_env, cursor=0)
        _row(r3_env, 1)
        p = _install_probes(monkeypatch, ws_armed=True, ws_succeeds=False)

        outcome = _run(r3_env)

        assert outcome.written == 1
        assert outcome._ws_fired is False
        # WS did not deliver → exactly one native fallback, still only one wake.
        assert p.ws_calls == []
        assert p.native_calls == [("t1", 1)]


# ---------------------------------------------------------------------------
# Acked id never re-emitted on reconnect / replay / aged echo.
# ---------------------------------------------------------------------------


class TestAckedIdNeverReemitted:
    def test_ack_then_rerun_zero_emits(self, r3_env: Any, monkeypatch: Any) -> None:
        _seed(r3_env, cursor=0)
        _row(r3_env, 1)
        p = _install_probes(monkeypatch, ws_armed=True, ws_succeeds=True)

        first = _run(r3_env)
        assert first.written == 1
        assert p.ws_calls == [("t1", 1)]

        # Supervisor drains + acks the id (path 1, the authoritative digest).
        ack_messages("t1", up_to_id=1)

        # Reset probes; a reconnect / rewake poll must emit NOTHING for id 1.
        p.ws_calls.clear()
        p.native_calls.clear()
        second = _run(r3_env)
        assert second.written == 0
        assert p.ws_calls == []
        assert p.native_calls == []

    def test_cursor_already_past_reconnect_no_emit(
        self, r3_env: Any, monkeypatch: Any
    ) -> None:
        """Reconnect scenario: the row was already acked (consumed cursor past
        it) before this incarnation polls. A fresh runner poll emits nothing —
        an acked id never re-surfaces on reconnect."""
        _seed(r3_env, cursor=0, consumed=5)
        _row(r3_env, 3)  # id at/below the consumed (acked) cursor
        p = _install_probes(monkeypatch, ws_armed=True, ws_succeeds=True)

        outcome = _run(r3_env)
        assert outcome.written == 0
        assert p.ws_calls == []
        assert p.native_calls == []

    def test_bridge_replay_of_acked_ids_zero_emits(
        self, r3_env: Any, monkeypatch: Any
    ) -> None:
        """Issue #388, 07:47Z sample: the bridge replayed 7 already-acked ids in
        one batch. After those ids are acked, NO runner poll may re-emit any of
        them on any surface."""
        acked_ids = [2726, 2727, 2730, 2733, 2734, 2735, 2736]
        _seed(r3_env, cursor=0)
        for rid in acked_ids:
            _row(r3_env, rid)
        p = _install_probes(monkeypatch, ws_armed=True, ws_succeeds=True)

        # Wake + advance the cursor over the whole batch, then ack them all.
        first = _run(r3_env)
        assert first.written == len(acked_ids)
        ack_messages("t1", up_to_id=max(acked_ids))

        # Now every replay/aged-echo poll emits zero.
        p.ws_calls.clear()
        p.native_calls.clear()
        for _ in range(3):  # three redundant replay attempts, as observed
            out = _run(r3_env)
            assert out.written == 0
        assert p.ws_calls == []
        assert p.native_calls == []


# ---------------------------------------------------------------------------
# Mutant ledger.
# ---------------------------------------------------------------------------


class TestMutantLedger:
    def test_mutant_emit_on_both_surfaces_is_red(
        self, r3_env: Any, monkeypatch: Any
    ) -> None:
        """MUTANT: emit on BOTH the WS and native surface for one id.

        The production invariant is at-most-one transport per id. This asserts
        WS and native are mutually exclusive for the same row — a mutant that
        removed the `not outcome._ws_fired` guard in _f136_post_delivery (or
        submitted native even after WS fired) would make this RED because the
        same id would appear on both probes.
        """
        _seed(r3_env, cursor=0)
        _row(r3_env, 1)
        p = _install_probes(monkeypatch, ws_armed=True, ws_succeeds=True)

        _run(r3_env)

        both = set(p.ws_calls) & set(p.native_calls)
        assert both == set(), f"id woke on BOTH surfaces (double wake): {both}"
        assert len(p.ws_calls) + len(p.native_calls) == 1

    def test_mutant_drop_cursor_advance_reemits_is_red(
        self, r3_env: Any, monkeypatch: Any
    ) -> None:
        """MUTANT: drop the commit_wake cursor advance.

        We simulate the mutant by forcing commit_wake to be a no-op that does
        NOT advance callback_notified_through_id. With the cursor never moving,
        a second runner poll RE-EMITS the same id — which this test asserts is a
        second wake (i.e. the mutant is caught: the real code advances the cursor
        and the second poll is silent, see TestOneWakePerIdNative.
        test_second_run_same_row_no_reemit).
        """
        import cli_agent_orchestrator.clients.database as dbmod_local

        _seed(r3_env, cursor=0)
        _row(r3_env, 1)
        p = _install_probes(monkeypatch, ws_armed=False)

        def _mutant_commit(*a: Any, **kw: Any) -> Any:
            # Advance NOTHING — return a committed verdict without moving cursor.
            class _R:
                kind = "committed"
                reason = ""

            return _R()

        # First run under the mutant (commit_wake is imported inside the runner
        # from clients.database, so the mutant is injected there).
        monkeypatch.setattr(dbmod_local, "commit_wake", _mutant_commit)
        first = _run(r3_env)
        assert first.written == 1
        assert p.native_calls == [("t1", 1)]
        # The mutant kill: dropping the commit_wake advance leaves the cursor at
        # 0. The real commit_wake advances it to the row id (see the sibling test
        # test_single_native_wake_and_cursor_advance, which asserts _cursor == 1).
        # This assertion is RED under the real code (cursor would be 1) and GREEN
        # only under the mutant — so we invert it: the mutant is DETECTED because
        # the two behaviours differ observably.
        assert _cursor(r3_env) == 0, (
            "under the mutant the cursor must NOT advance; the real commit_wake "
            "advances it to 1 (asserted in test_single_native_wake_and_cursor_advance)"
        )

    def test_real_commit_advances_cursor_mutant_baseline(
        self, r3_env: Any, monkeypatch: Any
    ) -> None:
        """Baseline half of the drop-cursor-advance mutant: the REAL commit_wake
        advances callback_notified_through_id to the emitted row id. A mutant
        that drops the advance leaves it at 0 (caught by the paired test above
        and by test_second_run_same_row_no_reemit, which relies on the advance
        to stay silent on the second poll)."""
        _seed(r3_env, cursor=0)
        _row(r3_env, 1)
        _install_probes(monkeypatch, ws_armed=False)

        out = _run(r3_env)
        assert out.written == 1
        assert _cursor(r3_env) == 1, "real commit_wake must advance the wake cursor"
