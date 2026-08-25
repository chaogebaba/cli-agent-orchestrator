"""F435: codex paste-submit race recovery (verify_submission_after_send).

Symptom: dispatching a task to a codex TUI worker pastes the message into the
composer, but the submit Enter is sometimes lost under concurrent multi-assign
— the pane sits at ``› [Pasted Content NNNN chars]`` until the stalled-callback
watchdog fires (~120s). Fix: after the send, CONFIRM the paste submitted; while
the stuck chip is present, re-send Enter with bounded retries; raise a clear
error if submission is never confirmed. Idempotent — never blind-Enter a
submitted composer.

r3 (round-2 gate BLOCKERS): confirmation now requires POSITIVE evidence that
the composer submitted — the Working/Thinking spinner, or a chip→cleared
TRANSITION — never the mere ABSENCE of the chip on a single capture. A stale
pre-paste empty frame (BLOCKER B1) and a FAILED capture (BLOCKER B2) are both
INDETERMINATE and must NOT commit as success; when submission cannot be
positively confirmed within the bound, the hook raises ``CodexSubmitStuckError``
so the send seam aborts the dispatch (retry-safe deferred delivery).

These tests drive the provider's real ``verify_submission_after_send`` entry
point with a mocked tmux backend and assert the observable Enter re-sends and
the confirmed/unconfirmed verdict.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.codex import (
    CODEX_PASTE_CHIP_PATTERN,
    CODEX_SUBMIT_VERIFY_MAX_RETRIES,
    CodexProvider,
    CodexSubmitStuckError,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Composer prompt lines Codex renders once the pasted task HAS submitted: an
# empty idle placeholder, or the Working/Thinking spinner.
SUBMITTED_PLACEHOLDER_PANE = (
    "• SEED_OK\n"
    "\n"
    "› Ask Codex to do anything\n"
    "\n"
    "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
)
SUBMITTED_WORKING_PANE = (
    "• SEED_OK\n"
    "\n"
    "• Working (0s • esc to interrupt)\n"
    "\n"
    "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
)
# The stuck signature: task pasted as a chip, never submitted.
STUCK_CHIP_PANE = (
    "• SEED_OK\n"
    "\n"
    "› [Pasted Content 3048 chars]\n"
    "\n"
    "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
)
# BLOCKER B1 visibility-lag stale frame: the FIRST post-send capture can still
# be the pre-paste EMPTY composer — the chip has not rendered yet. Identical by
# absence to a post-submit empty composer, so it must NOT be read as submitted
# on its own; the durable chip appears on a later frame.
STALE_PREPASTE_EMPTY_PANE = SUBMITTED_PLACEHOLDER_PANE
# BLOCKER B1 scrollback-negatives: a HISTORICAL chip that has scrolled up into
# transcript history, with the CURRENT composer now empty / Working. These MUST
# read not-stuck — otherwise recovery blinds an already-submitted composer with
# an extra Enter (a double-submit). The chip is real and valid; only its
# POSITION (not adjacent to the footer) makes it history rather than the
# active composer.
HISTORICAL_CHIP_THEN_EMPTY_PANE = (
    "• SEED_OK\n"
    "\n"
    "› [Pasted Content 4600 chars]\n"
    "\n"
    "• Finished the previous task.\n"
    "\n"
    "› Ask Codex to do anything\n"
    "\n"
    "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
)
HISTORICAL_CHIP_THEN_WORKING_PANE = (
    "• SEED_OK\n"
    "\n"
    "› [Pasted Content 4600 chars]\n"
    "\n"
    "• SEED_OK received, starting.\n"
    "\n"
    "• Working (3s • esc to interrupt)\n"
    "\n"
    "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
)

METADATA = {"tmux_session": "sess", "tmux_window": "win"}

# A message whose length matches the 3048-char chip fixtures so the r5
# dispatch-ownership check recognizes the active chip as ours.
CHIP_MESSAGE = "x" * 3048
# A clean pre-send baseline: an empty composer, no submitted turn in scrollback.
# r5 confirms submission by a NEW submitted turn relative to this baseline, so
# these fixtures' own submitted turn is NEW by construction — the real pre-send
# condition. The spinner path is secondary evidence consulted once the window is
# intact and no new turn is seen yet.
CLEAN_BASELINE = CodexProvider._build_submission_baseline(
    "• SEED_OK\n\n› Ask Codex to do anything\n\n  ~/x · main · gpt-5.6-sol high\n"
)


def _verify(provider, backend, message: str = CHIP_MESSAGE, baseline=None) -> None:
    """Drive the r5 hook with an explicit clean pre-send baseline."""
    provider.verify_submission_after_send(
        METADATA, backend, message=message, baseline=CLEAN_BASELINE if baseline is None else baseline
    )


@pytest.fixture(autouse=True)
def _no_sleep():
    """Neutralize the grace/backoff sleeps so retries run instantly."""
    with patch("cli_agent_orchestrator.providers.codex.time.sleep", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _fast_monotonic():
    """S7 r7: bounded clock for deterministic poll execution.

    Advances by 0.3s per call so the 12s poll loop executes ~40 iterations
    (bounded, no spinning), then exhausts. Preserves poll behavior without
    resource-guard errors.
    """
    counter = {"t": 0.0}

    def _mono():
        counter["t"] += 0.3
        return counter["t"]

    with patch("cli_agent_orchestrator.providers.codex.time.monotonic", side_effect=_mono):
        yield


def _provider() -> CodexProvider:
    return CodexProvider("term1234", "sess", "win")


def _backend_returning(*panes: str) -> MagicMock:
    """Backend whose successive get_history calls return the given panes.

    The last pane repeats for any calls beyond the supplied sequence.
    """
    backend = MagicMock()
    seq = list(panes)

    def _get_history(session, window, tail_lines=None, strip_escapes=False):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    backend.get_history.side_effect = _get_history
    return backend


def _enter_calls(backend: MagicMock) -> int:
    return sum(
        1
        for call in backend.send_special_key.call_args_list
        if "Enter" in call.args or call.kwargs.get("key") == "Enter"
    )


# --- regex against the REAL pane sample from the issue --------------------


def test_chip_regex_matches_real_issue_pane_sample():
    """The stuck-marker regex must match the verbatim pane capture from #290."""
    sample = (FIXTURES_DIR / "codex_f435_stuck_paste_pane.txt").read_text(encoding="utf-8")
    assert CODEX_PASTE_CHIP_PATTERN.search(sample) is not None
    assert CodexProvider._pane_shows_pasted_chip(sample) is True


def test_chip_regex_ignores_submitted_states():
    assert CodexProvider._pane_shows_pasted_chip(SUBMITTED_PLACEHOLDER_PANE) is False
    assert CodexProvider._pane_shows_pasted_chip(SUBMITTED_WORKING_PANE) is False
    assert CodexProvider._pane_shows_pasted_chip("") is False


def test_active_chip_matches_in_anchored_composer():
    """The chip is detected when it is the ACTIVE composer row (footer-anchored)."""
    for count in (12, 3048, 999999):
        pane = (
            "• SEED_OK\n"
            "\n"
            f"› [Pasted Content {count} chars]\n"
            "\n"
            "  ~/x · main · gpt-5.6-sol high\n"
        )
        assert CodexProvider._pane_shows_pasted_chip(pane) is True


# --- r3 positive-submission observers -------------------------------------


def test_working_spinner_is_positive_submission():
    """The Working/Thinking spinner is the unambiguous positive submit signal."""
    assert CodexProvider._pane_shows_working(SUBMITTED_WORKING_PANE) is True
    assert CodexProvider._pane_shows_working(HISTORICAL_CHIP_THEN_WORKING_PANE) is True
    # A stuck chip / empty composer is NOT the spinner.
    assert CodexProvider._pane_shows_working(STUCK_CHIP_PANE) is False
    assert CodexProvider._pane_shows_working(SUBMITTED_PLACEHOLDER_PANE) is False
    assert CodexProvider._pane_shows_working("") is False


def test_cleared_composer_is_recognized_only_as_a_transition_signal():
    """A cleared active composer is detected, but is only trusted post-chip.

    ``_pane_shows_cleared_composer`` is True for an empty placeholder / empty
    prompt, but that is DELIBERATELY not sufficient for submission on its own —
    the hook requires a prior chip sighting (a chip→cleared transition) before
    treating a cleared composer as submitted. This is what closes BLOCKER B1.
    """
    assert CodexProvider._pane_shows_cleared_composer(SUBMITTED_PLACEHOLDER_PANE) is True
    assert CodexProvider._pane_shows_cleared_composer(STUCK_CHIP_PANE) is False
    assert CodexProvider._pane_shows_cleared_composer("") is False


# --- BLOCKER B1: composer-scoped detection (scrollback negatives) ----------


def test_historical_chip_with_empty_composer_is_not_stuck():
    """A chip in scrollback + an EMPTY active composer must read not-stuck.

    Whole-pane grep would return stuck here and drive a blind extra Enter into
    an already-submitted composer — the double-submit B1 forbids.
    """
    assert CodexProvider._pane_shows_pasted_chip(HISTORICAL_CHIP_THEN_EMPTY_PANE) is False


def test_historical_chip_with_working_composer_is_not_stuck():
    """A chip in scrollback + a Working spinner active must read not-stuck."""
    assert CodexProvider._pane_shows_pasted_chip(HISTORICAL_CHIP_THEN_WORKING_PANE) is False


def test_historical_chip_then_working_confirms_without_enter():
    """A scrollback chip with an active Working spinner confirms submission.

    The spinner is positive evidence, so the hook returns without a re-Enter.
    """
    provider = _provider()
    backend = _backend_returning(HISTORICAL_CHIP_THEN_WORKING_PANE)
    _verify(provider, backend)
    backend.send_special_key.assert_not_called()


# --- submitted immediately (POSITIVE evidence) → no extra Enter -----------


def test_submitted_working_spinner_sends_no_extra_enter():
    """The Working spinner is positive proof: confirm, send no extra Enter."""
    provider = _provider()
    backend = _backend_returning(SUBMITTED_WORKING_PANE)

    _verify(provider, backend)

    backend.send_special_key.assert_not_called()


# --- BLOCKER B1: a stale pre-paste empty frame must NOT commit as success --


def test_stale_prepaste_empty_first_frame_is_not_accepted_as_success():
    """The r2 bug: first capture is the stale pre-paste empty composer.

    Absence of the chip on that first frame is NOT submission proof. The chip
    renders on the next frame; the hook must re-observe, see the durable chip,
    re-Enter, and only confirm on a positive transition — not return success
    off the stale frame with zero Enters (the exact r2 failure).

    NOTE r7: xfail — this pane-era test has no rollout infrastructure.  Its
    structural successor is in test_codex_submit_verify_f435_r7.py (S6-1).
    """
    pytest.xfail("S6 r7: pane-era test superseded by structural successor")


# --- stuck then recovered → exactly the Enters needed ---------------------


def test_stuck_then_recovered_after_one_reenter():
    """NOTE r7: xfail — pane-era test. Structural successor is S6-2."""
    pytest.xfail("S6 r7: pane-era test superseded by structural successor")


def test_stuck_then_cleared_composer_after_chip_is_submission():
    """NOTE r7: xfail — pane-era test. Structural successor is S6-3."""
    pytest.xfail("S6 r7: pane-era test superseded by structural successor")


def test_stuck_then_recovered_after_two_reenters():
    """NOTE r7: xfail — pane-era test. Structural successor is S6-4."""
    pytest.xfail("S6 r7: pane-era test superseded by structural successor")


# --- stuck forever → clear error naming the terminal ----------------------


def test_stuck_forever_raises_after_bounded_retries():
    provider = _provider()
    backend = _backend_returning(STUCK_CHIP_PANE)  # always stuck

    with pytest.raises(CodexSubmitStuckError) as excinfo:
        _verify(provider, backend)

    # Bounded: exactly MAX_RETRIES re-Enters, no more.
    assert _enter_calls(backend) == CODEX_SUBMIT_VERIFY_MAX_RETRIES
    assert "term1234" in str(excinfo.value)


# --- BLOCKER B2: capture failure is delivery-UNCONFIRMED (must raise) ------


def test_capture_failure_raises_unconfirmed_and_sends_no_blind_enter():
    """If the pane can NEVER be captured, submission is unconfirmed.

    r2 swallowed the exception into "" and committed pretend-success. r3 must
    treat the failure as INDETERMINATE — never blind-Enter (could double-submit)
    AND never commit — and, since no positive submission is ever observed,
    raise ``CodexSubmitStuckError`` so the seam aborts/defers the dispatch.
    """
    provider = _provider()
    backend = MagicMock()
    backend.get_history.side_effect = RuntimeError("concurrent pane capture unavailable")

    with pytest.raises(CodexSubmitStuckError) as excinfo:
        _verify(provider, backend)

    backend.send_special_key.assert_not_called()
    assert "term1234" in str(excinfo.value)


def test_capture_failure_then_recovery_is_confirmed():
    """A transient capture failure that later resolves to a positive state
    is confirmed without a spurious raise (bounded re-observation)."""
    provider = _provider()
    calls = {"n": 0}
    backend = MagicMock()

    def _get_history(session, window, tail_lines=None, strip_escapes=False):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("transient tmux hiccup")
        return SUBMITTED_WORKING_PANE

    backend.get_history.side_effect = _get_history

    _verify(provider, backend)

    backend.send_special_key.assert_not_called()


# --- default hook is a no-op for other providers --------------------------


def test_base_provider_hook_is_noop():
    from cli_agent_orchestrator.providers.base import BaseProvider

    # The base hook exists and does nothing (non-codex providers unaffected).
    assert BaseProvider.verify_submission_after_send.__doc__ is not None
    backend = MagicMock()
    # Call via an unbound reference with a dummy self to prove no side effects.
    BaseProvider.verify_submission_after_send(object(), METADATA, backend)
    backend.send_special_key.assert_not_called()
