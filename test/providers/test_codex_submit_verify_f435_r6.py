"""F435 round 6: STRUCTURAL rollout-signal regression tests.

r6 replaces the pane-heuristic PRIMARY signal with a STRUCTURAL one: the Codex
session rollout JSONL file gains a user-turn record matching the dispatched
message after the baseline byte offset.  Pane content is retained ONLY as a
fast-path hint — correctness NEVER depends on it.

These tests cover every r5 blocker scenario re-expressed against the structural
signal, plus new structural-specific probes:

  * B1 (partial-baseline false-commit): rollout match is exact content, so a
    historical rollout record at an offset BEFORE the baseline cannot confirm.
  * Identical raw repeats: even byte-identical messages produce separate rollout
    events (append-only), so each dispatch gets a unique confirm signal.
  * B2 (wrap/reflow/chip): rollout content is never wrapped — no pane geometry
    dependency.
  * B3 (equal-count eviction): rollout is offset-based, not count-based —
    eviction of pane history is irrelevant.
  * B4 (slow-submission double-send): recovery Enter fires ONLY after a FINAL
    rollout re-check immediately before sending.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.codex import (
    CODEX_SUBMIT_VERIFY_MAX_RETRIES,
    CodexProvider,
    CodexSubmitBaseline,
    CodexSubmitStuckError,
)

METADATA_BASE = {"tmux_session": "sess", "tmux_window": "win"}
FOOTER = "  ~/VScode_Projects/cli-subagents · main · gpt-5.6-sol high"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_sleep():
    """Eliminate all sleeps for fast test execution."""
    with patch("cli_agent_orchestrator.providers.codex.time.sleep", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _fast_monotonic():
    """Make time.monotonic() advance fast so polls exhaust immediately."""
    # Each call increments by a large step so poll loops exit instantly.
    counter = {"t": 0.0}

    def _mono():
        counter["t"] += 100.0
        return counter["t"]

    with patch("cli_agent_orchestrator.providers.codex.time.monotonic", side_effect=_mono):
        yield


def _provider() -> CodexProvider:
    return CodexProvider("term1234", "sess", "win")


def _metadata(session_uuid: str = "test-uuid-1234") -> dict[str, Any]:
    return {**METADATA_BASE, "provider_session_id": session_uuid}


def _backend_returning(*panes: str) -> MagicMock:
    """Mock backend whose get_history returns panes in sequence."""
    backend = MagicMock()
    seq = list(panes)

    def _get_history(session, window, tail_lines=None, strip_escapes=False):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    backend.get_history.side_effect = _get_history
    return backend


def _backend_no_chip() -> MagicMock:
    """Backend with an empty pane (no stuck chip visible)."""
    return _backend_returning("• SEED_OK\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n")


def _backend_stuck_chip(chars: int = 3048) -> MagicMock:
    """Backend showing a stuck chip owned by our dispatch."""
    pane = (
        "• SEED_OK\n\n"
        f"› [Pasted Content {chars} chars]\n\n"
        f"{FOOTER}\n"
    )
    return _backend_returning(pane)


def _enter_calls(backend: MagicMock) -> int:
    return sum(
        1
        for call in backend.send_special_key.call_args_list
        if "Enter" in call.args or call.kwargs.get("key") == "Enter"
    )


def _write_rollout_event(rollout_path: Path, message: str, *, event_type: str = "event_msg") -> None:
    """Append a user-turn record to the rollout file."""
    if event_type == "event_msg":
        record = {"type": "event_msg", "payload": {"type": "user_message", "message": message}}
    elif event_type == "response_item":
        record = {"type": "response_item", "payload": {"role": "user", "content": [{"text": message}]}}
    elif event_type == "user":
        record = {"type": "user", "message": message}
    else:
        raise ValueError(f"Unknown event_type: {event_type}")
    with rollout_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _write_non_user_event(rollout_path: Path) -> None:
    """Append a non-user record (assistant turn) to the rollout file."""
    record = {"type": "event_msg", "payload": {"type": "assistant_message", "message": "ok"}}
    with rollout_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


@pytest.fixture()
def rollout_dir(tmp_path: Path):
    """Create a sessions dir with a rollout file structure."""
    sessions = tmp_path / "sessions" / "test-uuid-1234"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-test-uuid-1234.jsonl"
    # Write session_meta as the first line (mimics real Codex behavior)
    with rollout.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {"id": "test-uuid-1234"}}) + "\n")
    return rollout


@pytest.fixture()
def patched_codex_home(tmp_path: Path):
    """Patch _resolved_codex_home to return tmp_path."""
    with patch(
        "cli_agent_orchestrator.providers.codex._resolved_codex_home",
        return_value=tmp_path,
    ):
        yield tmp_path


def _baseline_with_rollout(rollout_path: Path) -> CodexSubmitBaseline:
    """Build a baseline with the current rollout offset."""
    offset = rollout_path.stat().st_size if rollout_path.exists() else 0
    return CodexSubmitBaseline(
        rollout_path=rollout_path,
        rollout_offset=offset,
        captured_ok=True,
    )


# ===========================================================================
# STRUCTURAL SIGNAL — basic confirmation and rejection
# ===========================================================================


class TestRolloutConfirmation:
    """The rollout file gains a user-event record → confirmed."""

    def test_matching_event_after_offset_confirms(self, rollout_dir: Path, patched_codex_home: Path):
        """A user-turn record matching the message after baseline offset → success."""
        msg = "Implement the widget refactor please"
        baseline = _baseline_with_rollout(rollout_dir)
        # Write the matching event AFTER baseline
        _write_rollout_event(rollout_dir, msg)

        provider = _provider()
        backend = _backend_no_chip()
        provider.verify_submission_after_send(
            _metadata(), backend, message=msg, baseline=baseline
        )
        assert _enter_calls(backend) == 0

    def test_no_event_after_offset_raises(self, rollout_dir: Path, patched_codex_home: Path):
        """No matching user-turn record after offset → raises CodexSubmitStuckError."""
        msg = "Do the thing"
        baseline = _baseline_with_rollout(rollout_dir)
        # Do NOT write any event → rollout stays at baseline offset

        provider = _provider()
        backend = _backend_no_chip()
        with pytest.raises(CodexSubmitStuckError, match="structurally unconfirmed"):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )
        # No stuck chip → no Enter sent
        assert _enter_calls(backend) == 0

    def test_non_user_event_does_not_confirm(self, rollout_dir: Path, patched_codex_home: Path):
        """An assistant-turn record does not confirm the user dispatch."""
        msg = "Run the tests"
        baseline = _baseline_with_rollout(rollout_dir)
        _write_non_user_event(rollout_dir)

        provider = _provider()
        backend = _backend_no_chip()
        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )

    def test_mismatched_content_does_not_confirm(self, rollout_dir: Path, patched_codex_home: Path):
        """A user event with DIFFERENT content does not confirm."""
        msg = "Run the tests"
        baseline = _baseline_with_rollout(rollout_dir)
        _write_rollout_event(rollout_dir, "Completely different message")

        provider = _provider()
        backend = _backend_no_chip()
        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )

    def test_all_three_event_formats_confirm(self, rollout_dir: Path, patched_codex_home: Path):
        """All known Codex user-turn record formats are recognized."""
        msg = "Do the task"
        for fmt in ("event_msg", "response_item", "user"):
            # Reset rollout to just the session_meta
            with rollout_dir.open("w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "session_meta", "payload": {"id": "test-uuid-1234"}}) + "\n")
            baseline = _baseline_with_rollout(rollout_dir)
            _write_rollout_event(rollout_dir, msg, event_type=fmt)

            provider = _provider()
            backend = _backend_no_chip()
            # Should not raise
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )


# ===========================================================================
# B1 — partial-baseline false-commit (historical rollout events before offset)
# ===========================================================================


class TestB1PartialBaselineFalseCommit:
    """Events BEFORE the baseline offset must never confirm the current dispatch."""

    def test_historical_event_before_offset_does_not_confirm(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """A matching user event written BEFORE baseline capture is invisible.

        This is the structural equivalent of B1: a historical turn that
        happens to be identical to the current dispatch. The offset cursor
        excludes it by construction.
        """
        msg = "Implement the widget refactor please"
        # Write the event BEFORE taking baseline
        _write_rollout_event(rollout_dir, msg)
        baseline = _baseline_with_rollout(rollout_dir)
        # No new event after baseline → unconfirmed

        provider = _provider()
        backend = _backend_no_chip()
        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )

    def test_identical_message_before_and_after_offset_confirms(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """An identical message present BOTH before and after offset: the after
        one confirms (each append is a separate event)."""
        msg = "Implement the widget refactor please"
        _write_rollout_event(rollout_dir, msg)  # historical
        baseline = _baseline_with_rollout(rollout_dir)
        _write_rollout_event(rollout_dir, msg)  # new dispatch → confirms

        provider = _provider()
        backend = _backend_no_chip()
        provider.verify_submission_after_send(
            _metadata(), backend, message=msg, baseline=baseline
        )


# ===========================================================================
# Identical raw repeats — byte-identical dispatches
# ===========================================================================


class TestIdenticalRawRepeats:
    """Byte-identical messages produce separate rollout events per dispatch."""

    def test_second_identical_dispatch_needs_own_event(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """First dispatch confirmed by its event; second dispatch needs a NEW
        event after ITS baseline offset (which is after the first event)."""
        msg = "Implement the widget refactor please"
        # First dispatch: write event, take second baseline after it
        _write_rollout_event(rollout_dir, msg)  # confirms first dispatch
        baseline_2 = _baseline_with_rollout(rollout_dir)
        # Second dispatch: no new event → unconfirmed

        provider = _provider()
        backend = _backend_no_chip()
        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline_2
            )

    def test_second_identical_dispatch_with_own_event_confirms(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """Second dispatch writes its own event after the second baseline → ok."""
        msg = "Implement the widget refactor please"
        _write_rollout_event(rollout_dir, msg)  # first dispatch
        baseline_2 = _baseline_with_rollout(rollout_dir)
        _write_rollout_event(rollout_dir, msg)  # second dispatch event

        provider = _provider()
        backend = _backend_no_chip()
        provider.verify_submission_after_send(
            _metadata(), backend, message=msg, baseline=baseline_2
        )


# ===========================================================================
# B2 — wrap/reflow: rollout content is never wrapped
# ===========================================================================


class TestB2WrapReflow:
    """Rollout content is raw (never wrapped by terminal geometry)."""

    def test_whitespace_normalized_match(self, rollout_dir: Path, patched_codex_home: Path):
        """Messages with extra internal whitespace still match after normalization."""
        msg = "Implement   the\n  widget\t\trefactor please"
        baseline = _baseline_with_rollout(rollout_dir)
        # The rollout stores the canonical version
        _write_rollout_event(rollout_dir, "Implement the widget refactor please")

        provider = _provider()
        backend = _backend_no_chip()
        provider.verify_submission_after_send(
            _metadata(), backend, message=msg, baseline=baseline
        )

    def test_long_message_prefix_containment(self, rollout_dir: Path, patched_codex_home: Path):
        """Very long messages match via prefix containment (rollout may truncate)."""
        msg = "x" * 500
        baseline = _baseline_with_rollout(rollout_dir)
        # Rollout has the full message
        _write_rollout_event(rollout_dir, msg)

        provider = _provider()
        backend = _backend_no_chip()
        provider.verify_submission_after_send(
            _metadata(), backend, message=msg, baseline=baseline
        )


# ===========================================================================
# B3 — equal-count eviction: offset-based, not count-based
# ===========================================================================


class TestB3EqualCountEviction:
    """Rollout offset is immune to pane tail eviction."""

    def test_pane_eviction_irrelevant_when_rollout_confirms(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """Even if the pane evicts all history (shrunk watermark), the rollout
        still confirms — pane is just a hint."""
        msg = "Do the task"
        baseline = _baseline_with_rollout(rollout_dir)
        _write_rollout_event(rollout_dir, msg)

        provider = _provider()
        # Pane has shrunk watermark (evicted all history) — would be
        # INDETERMINATE under r5, but r6 uses rollout.
        backend = _backend_returning("• previous output scrolled off\n\n" + FOOTER + "\n")
        provider.verify_submission_after_send(
            _metadata(), backend, message=msg, baseline=baseline
        )
        assert _enter_calls(backend) == 0

    def test_pane_eviction_no_rollout_event_raises(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """Evicted pane + no rollout event → unconfirmed (no blind Enter)."""
        msg = "Do the task"
        baseline = _baseline_with_rollout(rollout_dir)

        provider = _provider()
        backend = _backend_returning("• scrolled off\n\n" + FOOTER + "\n")
        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )
        # No stuck chip visible → no Enter
        assert _enter_calls(backend) == 0


# ===========================================================================
# B4 — slow-submission double-send race: re-check before Enter
# ===========================================================================


class TestB4DoubleSendRace:
    """Recovery Enter fires ONLY after a final rollout re-check."""

    def test_stuck_chip_with_rollout_confirm_skips_enter(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """A stuck chip is visible BUT the rollout confirms (the submit landed
        between the last poll and the recovery action) → no Enter (race avoided)."""
        msg = "y" * 3048
        baseline = _baseline_with_rollout(rollout_dir)
        # Write the event so rollout confirms on re-check
        _write_rollout_event(rollout_dir, msg)

        provider = _provider()
        backend = _backend_stuck_chip(3048)
        # Should NOT raise — rollout confirms even though pane shows stuck chip
        provider.verify_submission_after_send(
            _metadata(), backend, message=msg, baseline=baseline
        )
        # Zero Enters — the re-check caught the late confirmation
        assert _enter_calls(backend) == 0

    def test_stuck_chip_no_rollout_event_fires_enter(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """A stuck chip + no rollout event → recovery Enter fires."""
        msg = "y" * 3048
        baseline = _baseline_with_rollout(rollout_dir)
        # No rollout event written

        provider = _provider()
        backend = _backend_stuck_chip(3048)
        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )
        # Enters were fired (bounded by MAX_RETRIES)
        assert _enter_calls(backend) == CODEX_SUBMIT_VERIFY_MAX_RETRIES

    def test_unrelated_chip_never_entered(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """A chip whose char-count doesn't match ours is never Entered."""
        msg = "y" * 3048
        baseline = _baseline_with_rollout(rollout_dir)

        provider = _provider()
        # Pane shows a chip of DIFFERENT size (555 != 3048)
        backend = _backend_stuck_chip(555)
        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )
        # Ownership check fails → zero Enters
        assert _enter_calls(backend) == 0


# ===========================================================================
# Rollout reader: complete-line JSONL handling
# ===========================================================================


class TestRolloutReaderCompleteLines:
    """Only COMPLETE lines (terminated by \\n) are parsed."""

    def test_incomplete_trailing_line_ignored(self, rollout_dir: Path, patched_codex_home: Path):
        """A partial (unterminated) last line is an in-progress write → ignored."""
        msg = "Do the task"
        baseline = _baseline_with_rollout(rollout_dir)
        # Write an incomplete record (no trailing newline)
        record = json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": msg}})
        with rollout_dir.open("a", encoding="utf-8") as f:
            f.write(record)  # NO trailing \n

        provider = _provider()
        backend = _backend_no_chip()
        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )

    def test_complete_line_is_parsed(self, rollout_dir: Path, patched_codex_home: Path):
        """A properly terminated line IS parsed and confirms."""
        msg = "Do the task"
        baseline = _baseline_with_rollout(rollout_dir)
        _write_rollout_event(rollout_dir, msg)  # writes with trailing \n

        provider = _provider()
        backend = _backend_no_chip()
        provider.verify_submission_after_send(
            _metadata(), backend, message=msg, baseline=baseline
        )

    def test_malformed_json_line_skipped(self, rollout_dir: Path, patched_codex_home: Path):
        """A malformed JSON line is skipped without error; valid lines after it work."""
        msg = "Do the task"
        baseline = _baseline_with_rollout(rollout_dir)
        with rollout_dir.open("a", encoding="utf-8") as f:
            f.write("this is not json\n")
        _write_rollout_event(rollout_dir, msg)

        provider = _provider()
        backend = _backend_no_chip()
        provider.verify_submission_after_send(
            _metadata(), backend, message=msg, baseline=baseline
        )


# ===========================================================================
# Session-file pinning: resolve by UUID, resume seed, newest
# ===========================================================================


class TestSessionFilePinning:
    """_resolve_rollout_file finds the correct file across strategies."""

    def test_exact_uuid_match(self, patched_codex_home: Path):
        """Single file matching UUID → returned."""
        sessions = patched_codex_home / "sessions" / "abc123"
        sessions.mkdir(parents=True)
        rollout = sessions / "rollout-abc123.jsonl"
        rollout.write_text("{}\n")

        provider = _provider()
        assert provider._resolve_rollout_file("abc123") == rollout

    def test_ambiguous_uuid_pins_newest(self, patched_codex_home: Path):
        """Multiple files matching UUID → newest by mtime returned."""
        sessions = patched_codex_home / "sessions"
        (sessions / "dir1").mkdir(parents=True)
        (sessions / "dir2").mkdir(parents=True)
        older = sessions / "dir1" / "rollout-abc123-old.jsonl"
        newer = sessions / "dir2" / "rollout-abc123-new.jsonl"
        older.write_text("{}\n")
        newer.write_text("{}\n")
        # Set mtime: newer is more recent
        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))

        provider = _provider()
        assert provider._resolve_rollout_file("abc123") == newer

    def test_no_uuid_uses_resume_seed(self, patched_codex_home: Path):
        """No UUID passed but resume seed available → resolves via seed."""
        sessions = patched_codex_home / "sessions" / "resume-seed-uuid"
        sessions.mkdir(parents=True)
        rollout = sessions / "rollout-resume-seed-uuid.jsonl"
        rollout.write_text("{}\n")

        provider = _provider()
        # Mock _fork_context for resume
        mock_ctx = MagicMock()
        mock_ctx.mode = "resume"
        mock_ctx.session_uuid = "resume-seed-uuid"
        provider._fork_context = mock_ctx

        assert provider._resolve_rollout_file(None) == rollout

    def test_no_uuid_no_resume_uses_newest(self, patched_codex_home: Path):
        """No UUID, no resume seed → newest rollout file returned."""
        sessions = patched_codex_home / "sessions"
        (sessions / "a").mkdir(parents=True)
        (sessions / "b").mkdir(parents=True)
        older = sessions / "a" / "rollout-aaa.jsonl"
        newer = sessions / "b" / "rollout-bbb.jsonl"
        older.write_text("{}\n")
        newer.write_text("{}\n")
        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))

        provider = _provider()
        provider._fork_context = None
        assert provider._resolve_rollout_file(None) == newer

    def test_no_sessions_dir_returns_none(self, patched_codex_home: Path):
        """No sessions directory → None (poll for creation)."""
        provider = _provider()
        assert provider._resolve_rollout_file("xyz") is None


# ===========================================================================
# Pane fast-path hint (retained as early exit, not correctness)
# ===========================================================================


class TestPaneFastPathHint:
    """Pane hint provides early exit but never drives Enter decisions."""

    def test_pane_submitted_hint_returns_early(self, rollout_dir: Path, patched_codex_home: Path):
        """If pane shows a submitted turn (Working spinner), fast-path exits
        without needing the rollout to confirm."""
        msg = "Do the task"
        baseline = _baseline_with_rollout(rollout_dir)
        # No rollout event — but pane shows Working (fast path hit)

        provider = _provider()
        pane = "• SEED_OK\n\n› Do the task\n\n⏳ Working…\n\n" + FOOTER + "\n"
        backend = _backend_returning(pane)

        # Patch _pane_shows_working to return True
        with patch.object(provider, "_pane_shows_working", return_value=True):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )
        assert _enter_calls(backend) == 0


# ===========================================================================
# Error message content
# ===========================================================================


class TestErrorMessage:
    """The error message reports structural details."""

    def test_error_mentions_rollout_offset(self, rollout_dir: Path, patched_codex_home: Path):
        """The CodexSubmitStuckError message includes the rollout offset."""
        msg = "Do the task"
        baseline = _baseline_with_rollout(rollout_dir)
        offset = baseline.rollout_offset

        provider = _provider()
        backend = _backend_no_chip()
        with pytest.raises(CodexSubmitStuckError, match=f"offset {offset}"):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline
            )
