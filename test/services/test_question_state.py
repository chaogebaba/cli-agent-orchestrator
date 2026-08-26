"""F507 question_state: edge markers + level parse + TTL (AC10, AC14, AC15)."""

import json
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.question_state import QuestionStateService


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _svc() -> tuple[QuestionStateService, _FakeClock]:
    clock = _FakeClock()
    return QuestionStateService(_clock=clock), clock


# ---- edge markers --------------------------------------------------------
def test_open_then_clear():
    svc, _ = _svc()
    assert svc.is_open("t1") is False
    svc.push_marker("t1", "question_open")
    assert svc.is_open("t1") is True
    svc.push_marker("t1", "question_clear")
    assert svc.is_open("t1") is False


def test_ac9_repeat_open_does_not_restart_ttl_clock():
    svc, clock = _svc()
    svc.push_marker("t1", "question_open")  # opened_at = 1000
    clock.advance(100.0)
    svc.push_marker("t1", "question_open")  # idempotent — must NOT reset opened_at
    # Reconcile with an unreadable transcript: TTL measured from the FIRST open.
    with patch.object(svc, "_parse_transcript", return_value=None):
        clock.advance(201.0)  # total 301 > 300 default TTL
        svc.reconcile("t1", {"id": "t1"})
    assert svc.is_open("t1") is False


# ---- layer-2 parse (AC15) ------------------------------------------------
def _write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_ac15_pending_tool_use_is_open(tmp_path):
    svc, _ = _svc()
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_1", "name": "AskUserQuestion"}
                    ],
                },
            },
        ],
    )
    assert svc._classify_jsonl(transcript) == "open"


def test_ac15_matched_tool_result_is_closed(tmp_path):
    svc, _ = _svc()
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_1", "name": "AskUserQuestion"}],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_1"}],
                },
            },
        ],
    )
    assert svc._classify_jsonl(transcript) == "closed"


def test_ac15_id_matching_not_positional(tmp_path):
    """A tool_use followed by a DIFFERENT tool's tool_result is still open."""
    svc, _ = _svc()
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_A", "name": "AskUserQuestion"}],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_B"}],
                },
            },
        ],
    )
    assert svc._classify_jsonl(transcript) == "open"


def test_ac15_no_tool_use_is_closed(tmp_path):
    svc, _ = _svc()
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        [{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text"}]}}],
    )
    assert svc._classify_jsonl(transcript) == "closed"


# ---- reconcile both ways (AC10) ------------------------------------------
def test_ac10_lost_clear_heals_from_transcript():
    """Open marker + transcript shows tool_result landed ⇒ reconcile clears it."""
    svc, _ = _svc()
    svc.push_marker("t1", "question_open")
    assert svc.is_open("t1") is True
    with patch.object(svc, "_parse_transcript", return_value="closed"):
        svc.reconcile("t1", {"id": "t1"})
    assert svc.is_open("t1") is False


def test_reconcile_opens_from_transcript_without_hook():
    svc, _ = _svc()
    assert svc.is_open("t1") is False
    with patch.object(svc, "_parse_transcript", return_value="open"):
        svc.reconcile("t1", {"id": "t1"})
    assert svc.is_open("t1") is True
    assert svc.source_layer("t1") == "transcript"


# ---- TTL / A1 tolerance (AC14) -------------------------------------------
def test_ac14_unreadable_holds_then_ttl_clears_once():
    svc, clock = _svc()
    svc.push_marker("t1", "question_open")
    with patch.object(svc, "_parse_transcript", return_value=None):
        # Holds across ticks while unreadable and within TTL.
        clock.advance(100.0)
        svc.reconcile("t1", {"id": "t1"})
        assert svc.is_open("t1") is True
        clock.advance(100.0)
        svc.reconcile("t1", {"id": "t1"})
        assert svc.is_open("t1") is True
        # Crosses 300s TTL ⇒ clears exactly once.
        clock.advance(101.0)
        with patch(
            "cli_agent_orchestrator.services.question_state.logger"
        ) as log:
            svc.reconcile("t1", {"id": "t1"})
            assert svc.is_open("t1") is False
            assert log.warning.call_count == 1
            # A subsequent tick does not re-warn (already cleared).
            svc.reconcile("t1", {"id": "t1"})
            assert log.warning.call_count == 1
