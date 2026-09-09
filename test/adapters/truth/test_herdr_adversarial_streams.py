"""Adversarial herdr event streams for the runtime source (WP-HERDR H1 r2, B1).

These are the codex EMPIRICAL-GATE probe streams from
``/data/cao-scratch/adj-herdr-h1/test_adversarial_streams.py``, shipped into the
suite so the two load-bearing mechanisms the r1 gate found untested are covered:

* the RESTART rebind (B1): herdr's ``terminal_id`` is new after every server
  restart, so the source must bind on the STORED stable ``agent_session`` and
  accept the new terminal identity only after that stable identity matches — the
  post-restart ``turn.ended`` must still project.  r1 bound on ``terminal_id``
  and this stream failed.
* the vocabulary streams (done→working, gap mid-turn, blocked burst) that assert
  the source emits ONLY the A2 boundary vocabulary and invents nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.adapters.truth import herdr_runtime
from cli_agent_orchestrator.adapters.truth.herdr_runtime import HerdrRuntimeSource
from cli_agent_orchestrator.core.events import EventDraft, EventKind


OLD_TID = "term_before_restart"
NEW_TID = "term_after_restart"
STABLE_SESSION = {
    "agent": "pi",
    "kind": "path",
    "source": "herdr:pi",
    "value": "/sessions/stable-agent-session.jsonl",
}


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> list[EventDraft]:
    emitted: list[EventDraft] = []
    runtime = SimpleNamespace(clock=SimpleNamespace(now=lambda: datetime(2026, 9, 9, tzinfo=UTC)))
    monkeypatch.setattr(herdr_runtime, "producer_runtime", lambda: runtime)
    monkeypatch.setattr(herdr_runtime, "emit", emitted.append)
    return emitted


def pane(status: str, *, terminal_id: str = OLD_TID) -> dict[str, object]:
    return {
        "pane_id": "w2:p1",
        "terminal_id": terminal_id,
        "agent": "pi",
        "agent_session": dict(STABLE_SESSION),
        "agent_status": status,
        "screen_detection_skipped": True,
        "state_change_seq": 10,
    }


def kinds(rows: list[EventDraft]) -> list[str]:
    return [row.kind.value for row in rows]


def push(source: HerdrRuntimeSource, record: dict[str, object]) -> None:
    source._handle_event({"event": "pane_updated", "data": {"pane": record}})


def test_done_then_working_uses_only_a2_vocabulary(rows: list[EventDraft]) -> None:
    source = HerdrRuntimeSource(OLD_TID, socket_path="/unused")
    push(source, pane("done"))
    push(source, pane("working"))
    assert kinds(rows) == [EventKind.TURN_ENDED.value, EventKind.TURN_STARTED.value]
    assert rows[0].payload["unseen_activity"] is True


def test_gap_mid_turn_emits_no_signal_then_resnapshot(rows: list[EventDraft]) -> None:
    source = HerdrRuntimeSource(OLD_TID, socket_path="/unused")
    push(source, pane("working"))
    source._emit_gap_degraded()
    push(source, pane("idle"))
    assert kinds(rows) == [
        EventKind.TURN_STARTED.value,
        EventKind.PANE_MISSING.value,
        EventKind.TURN_ENDED.value,
    ]
    assert rows[1].payload["reason"] == "no_signal"


def test_restart_rebinds_by_stable_agent_session_not_terminal_id(rows: list[EventDraft]) -> None:
    source = HerdrRuntimeSource(OLD_TID, socket_path="/unused")
    push(source, pane("working"))
    source._emit_gap_degraded()
    push(source, pane("idle", terminal_id=NEW_TID))
    assert kinds(rows) == [
        EventKind.TURN_STARTED.value,
        EventKind.PANE_MISSING.value,
        EventKind.TURN_ENDED.value,
    ]
    assert rows[-1].source_ref == "herdr:pi:/sessions/stable-agent-session.jsonl"


def test_blocked_burst_emits_nothing(rows: list[EventDraft]) -> None:
    source = HerdrRuntimeSource(OLD_TID, socket_path="/unused")
    for _ in range(5):
        push(source, pane("blocked"))
    assert rows == []
