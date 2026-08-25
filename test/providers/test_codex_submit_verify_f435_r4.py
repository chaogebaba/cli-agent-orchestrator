"""F435 round 4: the three r3-gate BLOCKER probes + negative controls.

The r3 gate (GATE-NO, 3 BLOCKER) proved the pane-state HEURISTICS ran out of
road: the two accepted positive states were observationally ambiguous and the
recovery loop could re-Enter after an Enter was already accepted. r4 anchors
submission on a DURABLE SCROLLBACK-CONTENT boundary — the pasted task appearing
as a SUBMITTED user turn (its ``[Pasted Content N chars]`` chip, or its raw
text, echoed as a ``›`` block ABOVE the active composer) — which is positive,
position-stable evidence in BOTH directions. Spinner/chip remain SECONDARY
corroboration.

Each probe below reproduces one r3 gate finding as an executable acceptance
test:

* ``E2`` (BLOCKER 1) — a FAST completed submit: the task submitted and finished
  before the first capture, so no spinner is ever observed and the active
  composer is already back to the placeholder, but the submitted turn is
  visible in scrollback. r3 classified this ``indeterminate`` → STUCK_ERROR →
  false defer/retry of an ALREADY-DELIVERED task. r4 must confirm from the
  scrollback boundary with ZERO re-Enters.

* ``B`` (BLOCKER 2) — a ``chip_seen → empty composer`` transition driven by an
  UNRELATED clear (composer wiped, no submit). r3 latched the transition as
  success (pretend-delivery of an unsent task). r4 must NOT treat a bare clear
  as submission: with no submitted turn in scrollback the verdict stays
  unconfirmed → raise.

* ``C`` (BLOCKER 3) — a STALE post-submit chip frame. After submission the
  scrollback shows the submitted turn, but one lagged capture still shows the
  chip on the active composer. r3 re-Entered on that stale chip → double
  delivery. r4 must read the scrollback boundary as "already submitted" and
  send NO further Enter.

Negative controls sit beside each probe so a mutant that simply flips a verdict
cannot pass: E2's control is a genuinely-stuck pane (must still raise); B's
control is a real submitted turn (must confirm); C's control is a truly stuck
chip with NO submitted turn (must re-Enter).
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.codex import (
    CodexProvider,
    CodexSubmitStuckError,
)

METADATA = {"tmux_session": "sess", "tmux_window": "win"}

# The task text CAO pasted for this dispatch. Large pastes collapse to a chip,
# so both a chip echo and a raw-text echo are exercised below.
TASK_TEXT = "Implement the widget refactor and report back with a diff."
# A message whose length matches the 3048-char chip fixtures, so the r5
# ownership check (active chip count ≈ len(message)) recognizes the chip as
# belonging to THIS dispatch.
CHIP_MESSAGE = "x" * 3048

# A clean pre-send baseline: an empty composer with NO submitted turn in
# scrollback — the real pane state immediately before a paste. Built from a
# capture the provider itself parses, so the dispatch's own submitted turn is
# NEW relative to it by construction.
CLEAN_PRESEND_PANE = (
    "• SEED_OK\n"
    "\n"
    "› Ask Codex to do anything\n"
    "\n"
    "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
)
CLEAN_BASELINE = CodexProvider._build_submission_baseline(CLEAN_PRESEND_PANE)


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("cli_agent_orchestrator.providers.codex.time.sleep", return_value=None):
        yield


def _provider() -> CodexProvider:
    return CodexProvider("term1234", "sess", "win")


def _backend_returning(*panes: str) -> MagicMock:
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


def _verify(
    provider: CodexProvider,
    backend: MagicMock,
    message: str = TASK_TEXT,
    baseline=None,
) -> None:
    """Drive the r5 hook with an explicit pre-send baseline (dispatch-relative).

    r5 confirms submission by a NEW submitted turn relative to a baseline
    captured BEFORE the paste. These probes pass ``CLEAN_BASELINE`` (an empty
    composer with no submitted turn) so the dispatch's own submitted turn is,
    by construction, NEW — exactly the real pre-send condition. A test that
    wants to model a pre-existing/historical turn passes its own baseline.
    """
    if baseline is None:
        baseline = CLEAN_BASELINE
    provider.verify_submission_after_send(
        METADATA, backend, message=message, baseline=baseline
    )


# ---------------------------------------------------------------------------
# Pane fixtures — the SUBMITTED-turn boundary the r4 model anchors on.
# ---------------------------------------------------------------------------

# A large paste that SUBMITTED: the chip has scrolled up into transcript
# history (a submitted user turn), the assistant answered, and the active
# composer at the bottom is back to the empty placeholder. No spinner is
# visible because the fast turn already finished (BLOCKER 1 / E2).
FAST_COMPLETED_SUBMIT_PANE = (
    "• SEED_OK\n"
    "\n"
    "› [Pasted Content 3048 chars]\n"
    "\n"
    "• Done — refactored the widget; diff below.\n"
    "\n"
    "› Ask Codex to do anything\n"
    "\n"
    "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
)

# Same submitted turn, but the paste was small enough to echo as RAW TEXT
# (not collapsed to a chip). Still a submitted `›` user block above the
# composer.
FAST_COMPLETED_SUBMIT_RAWTEXT_PANE = (
    "• SEED_OK\n"
    "\n"
    f"› {TASK_TEXT}\n"
    "\n"
    "• Done — refactored the widget; diff below.\n"
    "\n"
    "› Ask Codex to do anything\n"
    "\n"
    "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
)

# The stuck signature: the chip is on the ACTIVE composer row (adjacent to the
# footer) and there is NO submitted turn anywhere in scrollback.
STUCK_CHIP_PANE = (
    "• SEED_OK\n"
    "\n"
    "› [Pasted Content 3048 chars]\n"
    "\n"
    "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
)

# An empty active composer with NO submitted turn in scrollback — the pane a
# bare/unrelated clear leaves behind (BLOCKER 2 / B). Absence of a submitted
# turn ⇒ not submitted.
EMPTY_COMPOSER_NO_SUBMIT_PANE = (
    "• SEED_OK\n"
    "\n"
    "› Ask Codex to do anything\n"
    "\n"
    "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
)

# A genuine submitted turn (small raw-text paste) with the composer cleared —
# the B negative control: this MUST confirm.
SUBMITTED_TURN_THEN_EMPTY_PANE = FAST_COMPLETED_SUBMIT_RAWTEXT_PANE


# ===========================================================================
# BLOCKER 1 / E2 — fast completed submit must confirm, not defer.
# ===========================================================================


def test_E2_fast_completed_submit_confirms_without_reenter_chip_echo():
    """A task that submitted+finished before first capture is CONFIRMED.

    No spinner is observable and the active composer is already the empty
    placeholder, but the submitted turn (collapsed chip) sits in scrollback.
    r3 read this as indeterminate and raised after 16 captures (a false defer
    of a delivered task). r4 must confirm off the scrollback boundary and send
    zero re-Enters.
    """
    provider = _provider()
    backend = _backend_returning(FAST_COMPLETED_SUBMIT_PANE)

    _verify(provider, backend)  # must NOT raise

    assert _enter_calls(backend) == 0


def test_E2_fast_completed_submit_confirms_without_reenter_rawtext_echo():
    """Same as above but the paste echoed as raw text, not a chip."""
    provider = _provider()
    backend = _backend_returning(FAST_COMPLETED_SUBMIT_RAWTEXT_PANE)

    _verify(provider, backend)

    assert _enter_calls(backend) == 0


def test_E2_negative_control_truly_stuck_still_raises():
    """Negative control: a genuinely stuck chip (no submitted turn) must raise."""
    provider = _provider()
    backend = _backend_returning(STUCK_CHIP_PANE)

    with pytest.raises(CodexSubmitStuckError):
        _verify(provider, backend, message=CHIP_MESSAGE)


# ===========================================================================
# BLOCKER 2 / B — a bare clear is NOT submission evidence.
# ===========================================================================


def test_B_chip_then_unrelated_clear_does_not_falsely_confirm():
    """chip observed, then composer cleared by an UNRELATED wipe (no submit).

    There is no submitted turn in scrollback — only the active composer went
    from chip to empty. r3 latched that transition as success and committed an
    unsent task. r4 must NOT confirm: with re-Enters exhausted and still no
    submitted turn, it raises (retry-safe defer), never a pretend-success.
    """
    provider = _provider()
    # Frame 1: stuck chip (active composer). Frames 2+: composer cleared to the
    # empty placeholder with NOTHING submitted to scrollback.
    backend = _backend_returning(STUCK_CHIP_PANE, EMPTY_COMPOSER_NO_SUBMIT_PANE)

    with pytest.raises(CodexSubmitStuckError):
        _verify(provider, backend, message=CHIP_MESSAGE)


def test_B_negative_control_real_submitted_turn_confirms():
    """Negative control: a real submitted turn (chip→submitted-turn) must confirm.

    Frame 1 is the dispatch's own stuck chip; after the recovery Enter the chip
    has scrolled up into a SUBMITTED turn (new relative to the clean baseline)
    with the composer cleared. r5 confirms off that NEW submitted turn.
    """
    provider = _provider()
    recovered = (
        "• SEED_OK\n"
        "\n"
        "› [Pasted Content 3048 chars]\n"  # NEW submitted turn (history)
        "\n"
        "• On it.\n"
        "\n"
        "› Ask Codex to do anything\n"  # empty active composer
        "\n"
        "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
    )
    backend = _backend_returning(STUCK_CHIP_PANE, recovered)

    _verify(provider, backend, message=CHIP_MESSAGE)  # must NOT raise

    # Exactly the re-Enter that unstuck it — and no more.
    assert _enter_calls(backend) >= 1


# ===========================================================================
# BLOCKER 3 / C — a stale post-submit chip frame must not drive a re-Enter.
# ===========================================================================


def test_C_stale_postsubmit_chip_frame_sends_no_extra_enter():
    """A stale post-submit chip frame must not drive a SECOND Enter.

    Reproduces the gate's C probe: the first recovery Enter is accepted and the
    task submits (its turn now sits in scrollback), but the very next capture
    LAGS the TUI by one redraw and still shows the chip on the active composer
    with NO spinner yet. r3 read that stale frame as STUCK and sent a SECOND
    Enter — a double delivery. r4 checks the durable scrollback boundary FIRST:
    the submitted turn is present, so the pane is 'already submitted' and no
    further Enter is sent.

    On r3 this fails as ``enters == 2`` (or a raise); on r4 it is exactly the
    one Enter that submitted, and no more.
    """
    provider = _provider()
    # Frame 1: genuinely stuck (chip on active composer, nothing in history).
    # Frames 2+: a STALE post-submit frame — the submitted turn is now in
    # scrollback history, but the active composer STILL shows the chip (lagged
    # capture) and there is NO spinner to disambiguate.
    stale_after_submit = (
        "• SEED_OK\n"
        "\n"
        "› [Pasted Content 3048 chars]\n"  # submitted turn (history)
        "\n"
        "• Reading the task…\n"
        "\n"
        "› [Pasted Content 3048 chars]\n"  # STALE chip still on active composer
        "\n"
        "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
    )
    backend = _backend_returning(STUCK_CHIP_PANE, stale_after_submit)

    _verify(provider, backend, message=CHIP_MESSAGE)  # must NOT raise

    # r4 sends exactly the one Enter that submitted; the stale chip frame does
    # NOT trigger a second Enter (BLOCKER 3).
    assert _enter_calls(backend) == 1


def test_C_stale_chip_with_submitted_turn_confirms_without_any_enter():
    """A first-capture stale chip that already has a submitted turn in
    scrollback confirms with ZERO Enters.

    This is the purest B3 shape: the paste submitted on the ORIGINAL Enter, the
    turn is in history, yet the first capture still shows the chip on the active
    composer. r3 sends a (double-delivery) recovery Enter here; r4 reads the
    scrollback boundary and sends none.
    """
    provider = _provider()
    stale_first_frame = (
        "• SEED_OK\n"
        "\n"
        "› [Pasted Content 3048 chars]\n"  # submitted turn (history)
        "\n"
        "• Reading the task…\n"
        "\n"
        "› [Pasted Content 3048 chars]\n"  # STALE chip on active composer
        "\n"
        "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high\n"
    )
    backend = _backend_returning(stale_first_frame)

    _verify(provider, backend)  # must NOT raise

    assert _enter_calls(backend) == 0


def test_C_negative_control_stuck_chip_no_submitted_turn_reenters():
    """Negative control: a stuck chip with NO submitted turn in scrollback is
    genuinely stuck and MUST drive a re-Enter (then time out)."""
    provider = _provider()
    backend = _backend_returning(STUCK_CHIP_PANE)

    with pytest.raises(CodexSubmitStuckError):
        _verify(provider, backend, message=CHIP_MESSAGE)

    assert _enter_calls(backend) >= 1
