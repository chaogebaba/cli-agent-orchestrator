"""F802 (#658): codex recovery Enter is gated on chip/draft detection that never
matches, so the pasted task sits in the composer unsubmitted and the worker is
reaped at the 180 s deferred-init deadline.

Root cause (journal 2026-09-07 00:04-00:05 local, terminals 6cda991d / 2893a981
/ b311e027): ``verify_submission_after_send`` only re-sends Enter when
``_pane_shows_stuck_chip()`` (owned collapsed paste chip) OR
``_composer_holds_own_draft()`` (raw draft whose normalized signature matches)
is true. When a paste renders in the composer but neither owns it (chip length
mismatch under codex-cli split-paste; raw draft defeated by the assistant
SEED_OK bullet above it), the loop took the "no stuck chip visible; re-checking
rollout" branch — a backoff + rollout re-check ONLY. NO Enter was ever sent.
Every one of the 9 recovery attempts (3 dispatches x 3) logged that line, then
CodexSubmitStuckError -> DeliveryDeferredError -> exposure_crossed teardown.

Fix (tier-2, provider-local): when the rollout/SQLite store shows no user-turn
after the dispatch offset AND the pane shows no dialog / WAITING_USER_ANSWER AND
the active composer plainly holds unsubmitted content (not the idle placeholder,
not empty), send ONE Enter per recovery attempt regardless of chip/draft
ownership — bounded by the existing 3 attempts and the rollout double-send
guard. A dialog on screen still defers (fail-closed). The terminal
CodexSubmitStuckError message carries the last composer lines so the failure is
diagnosable from the callback.

Case (a) below is the RED repro: pre-fix it asserts zero Enter + raise; the fix
turns it green (Enter sent once, then confirmed).
"""

from __future__ import annotations

import json
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
from cli_agent_orchestrator.services.status_monitor import TerminalStatus

METADATA_BASE = {"tmux_session": "sess", "tmux_window": "win"}
FOOTER = "  ~/VScode_Projects/cli-subagents · main · gpt-5.6-sol high"


# ---------------------------------------------------------------------------
# Fixtures & helpers (mirror test_codex_submit_verify_f435_r6.py conventions)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("cli_agent_orchestrator.providers.codex.time.sleep", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _fast_monotonic():
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


def _backend_seq(*panes: str) -> MagicMock:
    """Backend whose get_history returns panes in sequence (last one sticks)."""
    backend = MagicMock()
    seq = list(panes)

    def _get_history(session, window, tail_lines=None, strip_escapes=False):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    backend.get_history.side_effect = _get_history
    return backend


def _pane_chip_mismatch(chars: int = 999) -> str:
    """F802 shape 1: a paste chip is present in the active composer, but its
    char-count does NOT match the dispatch length (codex-cli split-paste under-
    report). ``_pane_shows_stuck_chip`` fails ownership; ``_composer_holds_own_draft``
    reads only the chip chrome so its signature never matches the task text."""
    return "• SEED_OK\n\n" + f"› [Pasted Content {chars} chars]\n\n" + FOOTER + "\n"


def _pane_raw_draft(text: str) -> str:
    """F802 shape 2: the task sits in the composer as RAW text below the SEED_OK
    assistant bullet. ``read_composer_draft`` returns None (ownership-defer
    guard), so ``_composer_holds_own_draft`` is False."""
    return "• SEED_OK\n\n" + f"› {text}\n\n" + FOOTER + "\n"


def _pane_idle() -> str:
    return "• SEED_OK\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n"


def _pane_empty_glyph() -> str:
    """An empty composer: the bare ``›`` prompt glyph with no draft text."""
    return "• SEED_OK\n\n› \n\n" + FOOTER + "\n"


def _pane_working() -> str:
    """A pane that shows the turn is live (submission crossed)."""
    return "• SEED_OK\n\n• Working (0s)\n\n" + FOOTER + "\n"


def _enter_calls(backend: MagicMock) -> int:
    return sum(
        1
        for call in backend.send_special_key.call_args_list
        if "Enter" in call.args or call.kwargs.get("key") == "Enter"
    )


def _write_rollout_event(rollout_path: Path, message: str) -> None:
    record = {"type": "event_msg", "payload": {"type": "user_message", "message": message}}
    with rollout_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


@pytest.fixture()
def rollout_dir(tmp_path: Path):
    sessions = tmp_path / "sessions" / "test-uuid-1234"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-test-uuid-1234.jsonl"
    with rollout.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {"id": "test-uuid-1234"}}) + "\n")
    return rollout


@pytest.fixture()
def patched_codex_home(tmp_path: Path):
    with patch(
        "cli_agent_orchestrator.providers.codex._resolved_codex_home",
        return_value=tmp_path,
    ):
        yield tmp_path


def _baseline(rollout_path: Path) -> CodexSubmitBaseline:
    offset = rollout_path.stat().st_size if rollout_path.exists() else 0
    return CodexSubmitBaseline(
        rollout_path=rollout_path,
        rollout_offset=offset,
        captured_ok=True,
    )


def _not_waiting(_terminal_id: str) -> TerminalStatus:
    return TerminalStatus.IDLE


# ---------------------------------------------------------------------------
# (a) RED repro: paste visible, no chip owner, no rollout turn -> Enter once,
#     then confirmed.
# ---------------------------------------------------------------------------


class TestF802UnownedPasteRecovers:
    def test_chip_mismatch_paste_gets_recovery_enter_then_confirms(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """The exact F802 death shape: a paste chip whose length does not match
        the dispatch (ownership fails) with no rollout turn. Pre-fix: no Enter,
        raise. Post-fix: one Enter, and the rollout turn that lands after the
        Enter confirms delivery."""
        msg = "x" * 5000  # dispatch length !=  chip's 999-char count -> ownership fails
        baseline = _baseline(rollout_dir)

        def _get_history(session, window, tail_lines=None, strip_escapes=False):
            # First reads show the stuck (unowned) chip; once we've sent Enter,
            # the rollout event is written by the side_effect below.
            return _pane_chip_mismatch(999)

        backend = MagicMock()
        backend.get_history.side_effect = _get_history

        # Model the real TUI: the recovery Enter submits the paste, so the
        # rollout gains the matching user-turn immediately after the keypress.
        def _on_enter(session, window, key):
            if key == "Enter":
                _write_rollout_event(rollout_dir, msg)

        backend.send_special_key.side_effect = _on_enter

        provider = _provider()
        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            side_effect=_not_waiting,
        ):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline, first_dispatch=True
            )
        # Exactly one recovery Enter submitted the stuck paste.
        assert _enter_calls(backend) == 1

    def test_raw_draft_paste_gets_recovery_enter_then_confirms(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """F802 shape 2: the task sits as raw text below the SEED_OK bullet;
        read_composer_draft returns None so the F643c draft path never fired."""
        msg = "Please implement the widget refactor and run the full test suite"
        baseline = _baseline(rollout_dir)

        backend = MagicMock()
        backend.get_history.side_effect = lambda *a, **k: _pane_raw_draft(
            "Please implement the widget refactor and run tests"
        )

        def _on_enter(session, window, key):
            if key == "Enter":
                _write_rollout_event(rollout_dir, msg)

        backend.send_special_key.side_effect = _on_enter

        provider = _provider()
        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            side_effect=_not_waiting,
        ):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline, first_dispatch=True
            )
        assert _enter_calls(backend) == 1


# ---------------------------------------------------------------------------
# (b) rollout already has the turn -> no Enter (double-send guard).
# ---------------------------------------------------------------------------


class TestF802DoubleSendGuard:
    def test_rollout_confirmed_no_enter(self, rollout_dir: Path, patched_codex_home: Path):
        """If the original submit already landed in the rollout, the unowned-
        paste recovery must NOT fire — no blind second Enter."""
        msg = "Do the widget refactor"
        baseline = _baseline(rollout_dir)
        _write_rollout_event(rollout_dir, msg)  # already submitted

        # Even if the pane still shows a (stale) chip, rollout confirms first.
        backend = _backend_seq(_pane_chip_mismatch(999))
        provider = _provider()
        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            side_effect=_not_waiting,
        ):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline, first_dispatch=True
            )
        assert _enter_calls(backend) == 0


# ---------------------------------------------------------------------------
# (b2) #658 r2 B-2: the original submit lands DURING the first-dispatch branch —
#      after the branch-entry rollout check saw negative but BEFORE the recovery
#      Enter. The final re-check immediately before the Enter must catch it and
#      send ZERO Enters. Without that re-check the branch double-sends.
# ---------------------------------------------------------------------------


class TestF802FinalReCheckBeforeEnter:
    def test_rollout_appears_mid_branch_no_enter(self, rollout_dir: Path, patched_codex_home: Path):
        """Race: rollout confirms between the F802 branch entry and the Enter.

        The pane keeps showing the unowned stuck chip throughout, so the only
        thing that stops the recovery Enter is the B-2 final rollout re-check
        placed immediately before the keystroke. We drive it by controlling the
        provider's own ``_rollout_has_user_event`` — the substrate check backing
        ``_rollout_confirms()``:

          * call 1 = the branch-entry (F643c) re-check  -> False  (still stuck)
          * call 2 = the B-2 pre-Enter re-check         -> True   (submit landed)

        Post-fix: the B-2 re-check catches the landing and returns success with
        zero Enters. Pre-fix (no final re-check): the branch would send its Enter
        and duplicate the just-landed submission.
        """
        msg = "Implement the widget refactor and run the full suite now"
        baseline = _baseline(rollout_dir)

        backend = _backend_seq(_pane_chip_mismatch(999))
        provider = _provider()

        calls = {"n": 0}

        def _rollout_seq(*_a: Any, **_k: Any) -> bool:
            calls["n"] += 1
            # First re-check (branch entry) is still negative; the original
            # submit lands in the window, so the second re-check (pre-Enter)
            # confirms.
            return calls["n"] >= 2

        with (
            patch(
                "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
                side_effect=_not_waiting,
            ),
            patch.object(provider, "_rollout_has_user_event", side_effect=_rollout_seq),
        ):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=baseline, first_dispatch=True
            )
        # The pre-Enter re-check caught the landed submit: zero recovery Enters.
        assert _enter_calls(backend) == 0
        # And the re-check was actually consulted a second time (the B-2 gate).
        assert calls["n"] >= 2


# ---------------------------------------------------------------------------
# (c) dialog visible -> defer, no Enter (fail-closed).
# ---------------------------------------------------------------------------


class TestF802DialogFailsClosed:
    def test_waiting_user_answer_defers_without_enter(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """A blocking dialog (WAITING_USER_ANSWER) owns the pane and absorbed the
        submit Enter. The recovery must NOT blind-Enter into a dialog; it defers
        to the auto-responder re-arm loop, which here never clears -> raise, and
        crucially sends ZERO composer-recovery Enters."""
        msg = "Do the widget refactor"
        baseline = _baseline(rollout_dir)

        backend = _backend_seq(_pane_chip_mismatch(999))
        provider = _provider()

        with (
            patch(
                "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
                return_value=TerminalStatus.WAITING_USER_ANSWER,
            ),
            patch(
                "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
                return_value=["dialog"],
            ),
            patch(
                "cli_agent_orchestrator.services.auto_responder.auto_responder.on_screen",
                return_value=None,
            ),
        ):
            with pytest.raises(CodexSubmitStuckError):
                provider.verify_submission_after_send(
                    _metadata(), backend, message=msg, baseline=baseline, first_dispatch=True
                )
        assert _enter_calls(backend) == 0

    def test_empty_glyph_composer_never_enters(self, rollout_dir: Path, patched_codex_home: Path):
        """An empty composer (bare ``›`` glyph, no text) is NOT unsubmitted
        content — even on first_dispatch it must send zero Enters (submitting an
        empty prompt would be wrong). Kills a mutant that drops the empty-body
        guard in _composer_holds_unsubmitted_text."""
        msg = "Do the widget refactor now"
        baseline = _baseline(rollout_dir)

        backend = _backend_seq(_pane_empty_glyph())
        provider = _provider()
        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            side_effect=_not_waiting,
        ):
            with pytest.raises(CodexSubmitStuckError):
                provider.verify_submission_after_send(
                    _metadata(), backend, message=msg, baseline=baseline, first_dispatch=True
                )
        assert _enter_calls(backend) == 0

    def test_branch_level_dialog_guard_blocks_enter(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """Defense-in-depth: even if the loop-entry dialog pre-check saw IDLE, a
        dialog that appears by the time the F802 branch runs must still block the
        recovery Enter (fail-closed at the branch, not only at the pre-check).
        Kills a mutant that inverts the branch-level WAITING_USER_ANSWER guard."""
        msg = "Do the widget refactor now please"
        baseline = _baseline(rollout_dir)

        backend = _backend_seq(_pane_chip_mismatch(999))
        provider = _provider()
        # Pre-check reads IDLE once; each of the 3 branch checks reads WAITING.
        statuses = [TerminalStatus.IDLE] + [TerminalStatus.WAITING_USER_ANSWER] * 6

        def _status_seq(_terminal_id: str) -> TerminalStatus:
            return statuses.pop(0) if len(statuses) > 1 else statuses[0]

        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            side_effect=_status_seq,
        ):
            with pytest.raises(CodexSubmitStuckError):
                provider.verify_submission_after_send(
                    _metadata(), backend, message=msg, baseline=baseline, first_dispatch=True
                )
        assert _enter_calls(backend) == 0


class TestF802ErrorCarriesComposerExcerpt:
    def test_unconfirmed_error_includes_composer_lines(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """When all recovery attempts fail to confirm, the terminal error must
        include the last composer lines so the failure is diagnosable from the
        callback (the reaped pane is otherwise gone)."""
        msg = "Diagnose the flaky integration test in the widget module"
        baseline = _baseline(rollout_dir)

        distinctive = "UNSUBMITTED-COMPOSER-MARKER-42"
        # Enter is sent but never confirms (rollout stays empty).
        backend = _backend_seq(_pane_raw_draft(distinctive))
        provider = _provider()
        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            side_effect=_not_waiting,
        ):
            with pytest.raises(CodexSubmitStuckError) as excinfo:
                provider.verify_submission_after_send(
                    _metadata(), backend, message=msg, baseline=baseline, first_dispatch=True
                )
        text = str(excinfo.value)
        assert "structurally unconfirmed" in text
        # The composer excerpt must be present for diagnosis.
        assert distinctive in text

    def test_unowned_paste_bounded_to_three_enters(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """The blind recovery Enter is bounded by CODEX_SUBMIT_VERIFY_MAX_RETRIES
        — never an unbounded resend loop."""
        msg = "Diagnose the flaky integration test"
        baseline = _baseline(rollout_dir)

        backend = _backend_seq(_pane_raw_draft("some unsubmitted task text here"))
        provider = _provider()
        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            side_effect=_not_waiting,
        ):
            with pytest.raises(CodexSubmitStuckError):
                provider.verify_submission_after_send(
                    _metadata(), backend, message=msg, baseline=baseline, first_dispatch=True
                )
        assert _enter_calls(backend) <= CODEX_SUBMIT_VERIFY_MAX_RETRIES

    def test_idle_placeholder_never_enters(self, rollout_dir: Path, patched_codex_home: Path):
        """Regression guard: an empty/idle composer (placeholder only) is NOT
        unsubmitted content — even on the first-dispatch path the recovery must
        never Enter into it (that would submit an empty prompt)."""
        msg = "Do the thing"
        baseline = _baseline(rollout_dir)

        backend = _backend_seq(_pane_idle())
        provider = _provider()
        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            side_effect=_not_waiting,
        ):
            with pytest.raises(CodexSubmitStuckError):
                provider.verify_submission_after_send(
                    _metadata(), backend, message=msg, baseline=baseline, first_dispatch=True
                )
        assert _enter_calls(backend) == 0


# ---------------------------------------------------------------------------
# Option-1 guard (supervisor ruling): a NORMAL (non-first-dispatch) send with an
# unowned composer must still send ZERO Enters — the relaxation is scoped to the
# deferred-init/first-dispatch path only.
# ---------------------------------------------------------------------------


class TestF802NormalSendStaysStrict:
    def test_normal_send_unowned_chip_no_enter(self, rollout_dir: Path, patched_codex_home: Path):
        """first_dispatch defaults False on send_input/send_prepared_input. A
        composer holding an UNOWNED chip on a normal send must NOT be Entered —
        the strict-ownership guards (r5/r6/f643c/split-paste) are untouched."""
        msg = "y" * 5000  # length != the 999-char chip -> ownership fails
        baseline = _baseline(rollout_dir)

        backend = _backend_seq(_pane_chip_mismatch(999))
        provider = _provider()
        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            side_effect=_not_waiting,
        ):
            with pytest.raises(CodexSubmitStuckError):
                # No first_dispatch kwarg -> defaults False (normal send).
                provider.verify_submission_after_send(
                    _metadata(), backend, message=msg, baseline=baseline
                )
        assert _enter_calls(backend) == 0

    def test_normal_send_unowned_raw_draft_no_enter(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """A raw unrelated draft on a normal send is never blind-Entered."""
        msg = "Implement the widget refactor and run the full suite"
        baseline = _baseline(rollout_dir)

        backend = _backend_seq(_pane_raw_draft("a completely unrelated note a human typed"))
        provider = _provider()
        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            side_effect=_not_waiting,
        ):
            with pytest.raises(CodexSubmitStuckError):
                provider.verify_submission_after_send(
                    _metadata(), backend, message=msg, baseline=baseline, first_dispatch=False
                )
        assert _enter_calls(backend) == 0
