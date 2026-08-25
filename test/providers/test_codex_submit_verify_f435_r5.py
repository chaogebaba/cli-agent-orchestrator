"""F435 round 5: the three r4-gate BLOCKER probes + negative controls.

The r4 gate (GATE-NO, 3 BLOCKER) proved the SCROLLBACK-ANCHOR heuristic still
had no DISPATCH-RELATIVE boundary: ``_pane_shows_submitted_task`` scanned ALL
captured history with no pre-send cursor, so

  * B1 — any historical paste chip (same/different N), an identical prior task,
    or an identical 40-char prefix false-confirmed the CURRENT unsent chip;
  * B2 — wrapped raw continuations were discarded and a wrapped active chip
    broke the footer anchor, while a <12-char fast completion produced no
    signature and false-deferred;
  * B3 — a 200-row tail could evict the evidence, and an unrelated active draft
    absorbed recovery Enters mid-run.

r5 makes the evidence DISPATCH-RELATIVE by BASELINE DIFF: a baseline of the
pane is captured immediately BEFORE the paste (``capture_submission_baseline``);
submission is confirmed ONLY by a submitted user turn that is NEW relative to
that baseline (``_pane_shows_new_submitted_task``). Historical collisions live
in the baseline and are excluded BY CONSTRUCTION.

Each probe below reproduces one r4 gate finding as an executable acceptance
test. Because the r4 hook has NO ``baseline`` parameter, the r4 implementation
cannot even express these scenarios — that is the point: the discrimination
r4 lacked (current-vs-historical) is exactly what the baseline supplies.

Negative controls sit beside each probe so a mutant that ignores the baseline
(reverting to r4's absolute scan) turns RED: the control's expected verdict is
the OPPOSITE of the probe's.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.codex import (
    CODEX_SUBMIT_STATE_INDETERMINATE,
    CODEX_SUBMIT_STATE_SUBMITTED,
    CODEX_SUBMIT_VERIFY_MAX_RETRIES,
    CodexProvider,
    CodexSubmitStuckError,
)

METADATA = {"tmux_session": "sess", "tmux_window": "win"}

FOOTER = "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("cli_agent_orchestrator.providers.codex.time.sleep", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _fast_monotonic():
    """S7 r7: bounded clock for deterministic poll execution."""
    counter = {"t": 0.0}

    def _mono():
        counter["t"] += 0.3
        return counter["t"]

    with patch("cli_agent_orchestrator.providers.codex.time.monotonic", side_effect=_mono):
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


def _baseline_from(pane: str):
    return CodexProvider._build_submission_baseline(pane)


def _new_task(pane: str, message, baseline) -> str:
    return CodexProvider._pane_shows_new_submitted_task(pane, message, baseline)


# ---------------------------------------------------------------------------
# The dispatch under test: a large paste (collapses to a chip) whose length is
# large, plus a shorter raw task. The pre-send pane already carries HISTORICAL
# collisions (identical/prefix/same-N/different-N) — the B1 workload.
# ---------------------------------------------------------------------------

TASK_TEXT = "Implement the widget refactor and report back with a diff, please."
TASK_LEN = len(TASK_TEXT)
CHIP_MESSAGE = "y" * 3048


def _pane_with_active_chip(history_rows: str, chars: int = 3048) -> str:
    """Build a pane: history rows, then the ACTIVE composer carrying our chip."""
    return (
        "• SEED_OK\n"
        "\n"
        f"{history_rows}"
        f"› [Pasted Content {chars} chars]\n"  # ACTIVE composer (adjacent to footer)
        "\n"
        f"{FOOTER}\n"
    )


def _pane_with_active_raw(history_rows: str, draft: str) -> str:
    return (
        "• SEED_OK\n"
        "\n"
        f"{history_rows}"
        f"› {draft}\n"
        "\n"
        f"{FOOTER}\n"
    )


# ===========================================================================
# BLOCKER 1 — historical collisions must NOT confirm the current unsent chip.
# ===========================================================================


def test_A1_prior_identical_raw_does_not_confirm_current_unsent_chip():
    """A prior IDENTICAL raw turn in history + our chip still unsent.

    r4 false-confirmed (the prefix matched a historical row). r5 puts that
    historical turn in the BASELINE, so it is not NEW → not confirmed; the
    current chip owns the composer and is still stuck.
    """
    prior = f"› {TASK_TEXT}\n\n• Done earlier.\n\n"
    presend = (
        "• SEED_OK\n\n" + prior + "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    baseline = _baseline_from(presend)
    # POST-send: our chip is on the active composer; the identical prior turn is
    # still up in history, unchanged.
    post = _pane_with_active_chip(prior, chars=len(CHIP_MESSAGE))

    assert _new_task(post, CHIP_MESSAGE, baseline) == ""  # not a NEW turn
    provider = _provider()
    backend = _backend_returning(post)
    with pytest.raises(CodexSubmitStuckError):
        provider.verify_submission_after_send(
            METADATA, backend, message=CHIP_MESSAGE, baseline=baseline
        )
    # It IS recognized as our stuck chip → recovery Enters fired (bounded).
    assert _enter_calls(backend) == CODEX_SUBMIT_VERIFY_MAX_RETRIES


def test_A2_prior_40prefix_collision_does_not_confirm():
    """A prior turn sharing our 40-char prefix but a different tail.

    r4 matched the normalized 40-char prefix anywhere in history → false
    confirm. r5: the prior turn is in the baseline; only a NEW turn carrying
    the signature confirms.
    """
    prior_same_prefix = TASK_TEXT[:40] + " but an entirely different older request."
    prior = f"› {prior_same_prefix}\n\n• Answered long ago.\n\n"
    presend = (
        "• SEED_OK\n\n" + prior + "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    baseline = _baseline_from(presend)
    post = _pane_with_active_raw(prior, draft=TASK_TEXT)  # our raw draft, unsent

    # The active draft is on the composer (not history); the only history turn
    # is the baseline one → no NEW turn.
    assert _new_task(post, TASK_TEXT, baseline) == ""


def test_C1_prior_same_count_chip_does_not_confirm():
    """A prior chip with the SAME char-count as ours, already in history.

    r4 confirmed off the first historical [Pasted Content N] with no N compare.
    r5: the baseline holds one chip of that N; only a SECOND (count-exceeding)
    occurrence is NEW.
    """
    prior = "› [Pasted Content 3048 chars]\n\n• Prior identical-size paste.\n\n"
    presend = (
        "• SEED_OK\n\n" + prior + "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    baseline = _baseline_from(presend)
    assert baseline.chip_count("[Pasted Content 3048 chars]") == 1
    # POST: our own 3048 chip still drafted on the active composer; history
    # unchanged (still exactly one submitted 3048 chip).
    post = _pane_with_active_chip(prior, chars=3048)

    assert _new_task(post, CHIP_MESSAGE, baseline) == ""  # count did not exceed


def test_C2_prior_different_count_chip_does_not_confirm():
    """A prior chip of a DIFFERENT char-count in history.

    r4 also confirmed off this (N never compared). r5: a different-N historical
    chip is simply an unrelated baseline entry; our unsent chip is not NEW.
    """
    prior = "› [Pasted Content 999 chars]\n\n• Prior smaller paste.\n\n"
    presend = (
        "• SEED_OK\n\n" + prior + "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    baseline = _baseline_from(presend)
    post = _pane_with_active_chip(prior, chars=len(CHIP_MESSAGE))

    assert _new_task(post, CHIP_MESSAGE, baseline) == ""


def test_exact_length_control_prior_same_and_other_count_both_stay_unconfirmed():
    """The gate's exact-length control: prior N == our len AND prior N != our len.

    Both prior chips are historical (baseline); neither makes our unsent chip a
    NEW turn.
    """
    for prior_n in (3048, 999):
        prior = f"› [Pasted Content {prior_n} chars]\n\n• older.\n\n"
        presend = "• SEED_OK\n\n" + prior + "› Ask Codex to do anything\n\n" + FOOTER + "\n"
        baseline = _baseline_from(presend)
        post = _pane_with_active_chip(prior, chars=3048)
        assert _new_task(post, CHIP_MESSAGE, baseline) == ""


def test_B1_negative_control_new_chip_beyond_baseline_confirms():
    """Negative control: once our chip SUBMITS, a NEW same-N chip occurrence
    appears in history beyond the baseline count → confirmed.

    Distinguishes 'a new current turn' from 'some matching old turn': the same
    pane that stays unconfirmed while the chip is drafted MUST confirm once a
    second (submitted) occurrence exists.
    """
    prior = "› [Pasted Content 3048 chars]\n\n• Prior identical-size paste.\n\n"
    presend = "• SEED_OK\n\n" + prior + "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    baseline = _baseline_from(presend)
    assert baseline.chip_count("[Pasted Content 3048 chars]") == 1
    # POST-submit: a SECOND submitted 3048 chip (ours) now in history, composer
    # cleared.
    post = (
        "• SEED_OK\n\n"
        + prior
        + "› [Pasted Content 3048 chars]\n\n• On it.\n\n"  # our NEW submitted turn
        "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    assert _new_task(post, CHIP_MESSAGE, baseline) == CODEX_SUBMIT_STATE_SUBMITTED


def test_B1_negative_control_new_raw_turn_with_signature_confirms():
    """Negative control: a NEW raw turn carrying our signature confirms even
    when a prefix-colliding historical turn is present in the baseline."""
    prior_same_prefix = TASK_TEXT[:40] + " but an entirely different older request."
    prior = f"› {prior_same_prefix}\n\n• Answered long ago.\n\n"
    presend = "• SEED_OK\n\n" + prior + "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    baseline = _baseline_from(presend)
    post = (
        "• SEED_OK\n\n"
        + prior
        + f"› {TASK_TEXT}\n\n• Working on it.\n\n"  # our NEW submitted turn
        "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    assert _new_task(post, TASK_TEXT, baseline) == CODEX_SUBMIT_STATE_SUBMITTED


# ===========================================================================
# BLOCKER 2 — wrapping and short tasks.
# ===========================================================================


def test_W1_wrapped_raw_submitted_turn_is_recognized():
    """A genuine submitted raw turn that SOFT-WRAPPED across visual rows.

    r4 discarded continuation rows (kept only ``›``-prefixed rows), so a
    40-char signature split across rows never matched → false STUCK/defer. r5
    joins the ``›`` head with its wrapped continuation rows before normalizing,
    so the signature matches.
    """
    presend = "• SEED_OK\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n"
    baseline = _baseline_from(presend)
    # The submitted turn wrapped: the head row holds the first slice, the
    # continuation row (no ``›``) holds the rest.
    head = "› " + TASK_TEXT[:25]
    cont = TASK_TEXT[25:]
    post = (
        "• SEED_OK\n\n"
        f"{head}\n"
        f"{cont}\n"
        "\n• Working on it.\n\n"
        "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    assert _new_task(post, TASK_TEXT, baseline) == CODEX_SUBMIT_STATE_SUBMITTED


def test_W1_negative_control_wrapped_unrelated_turn_does_not_confirm():
    """Negative control: a wrapped submitted turn that is NOT ours (no
    signature) must not confirm our dispatch."""
    presend = "• SEED_OK\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n"
    baseline = _baseline_from(presend)
    other = "Totally unrelated question about the weather forecast for tomorrow"
    post = (
        "• SEED_OK\n\n"
        f"› {other[:25]}\n{other[25:]}\n"
        "\n• answered.\n\n"
        # our chip still drafted on the active composer:
        "› [Pasted Content 3048 chars]\n\n" + FOOTER + "\n"
    )
    assert _new_task(post, TASK_TEXT, baseline) == ""


def test_short_task_still_confirms_from_new_turn():
    """The gate's short fast-complete: a <12-char raw task ("Say ok").

    r4 produced no signature (min 12 chars) → could only confirm via chip, and
    a short raw echo never matched → false STUCK after 16 captures. r5: a short
    task still produces a NEW submitted ``›`` turn absent from the baseline, so
    it confirms with zero re-Enters even without a signature.
    """
    msg = "Say ok"  # 6 chars
    presend = "• SEED_OK\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n"
    baseline = _baseline_from(presend)
    post = (
        "• SEED_OK\n\n"
        "› Say ok\n\n• ok\n\n"  # NEW submitted short turn
        "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    assert _new_task(post, msg, baseline) == CODEX_SUBMIT_STATE_SUBMITTED

    provider = _provider()
    backend = _backend_returning(post)
    provider.verify_submission_after_send(METADATA, backend, message=msg, baseline=baseline)
    assert _enter_calls(backend) == 0


def test_short_task_negative_control_no_new_turn_defers():
    """Negative control: a short task whose turn NEVER appears (drafted only)
    must not confirm — the window is intact and no new turn exists."""
    msg = "Say ok"
    presend = "• SEED_OK\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n"
    baseline = _baseline_from(presend)
    # Short raw draft still on the active composer; nothing submitted.
    post = "• SEED_OK\n\n› Say ok\n\n" + FOOTER + "\n"
    assert _new_task(post, msg, baseline) == ""


def test_short_11_and_12_char_controls_both_confirm_from_new_turn():
    """The gate's 11-char vs 12-char boundary controls.

    r4: 11 chars → no signature → STUCK_ERROR; 12 chars → SUCCESS (a brittle
    threshold artifact). r5: BOTH confirm from the NEW submitted turn — the
    signature threshold no longer decides delivery, the baseline diff does.
    """
    for length in (11, 12):
        msg = "z" * length
        presend = "• SEED_OK\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n"
        baseline = _baseline_from(presend)
        post = (
            "• SEED_OK\n\n"
            f"› {msg}\n\n• ack\n\n"
            "› Ask Codex to do anything\n\n" + FOOTER + "\n"
        )
        assert _new_task(post, msg, baseline) == CODEX_SUBMIT_STATE_SUBMITTED


# ===========================================================================
# BLOCKER 3 — eviction watermark and unrelated-draft ownership.
# ===========================================================================


def test_E1_eviction_is_indeterminate_not_a_blind_enter():
    """The gate's E1: the current submitted turn is EVICTED past the capture
    tail and an UNRELATED active chip owns the composer.

    r4 read the missing boundary as STUCK and drove 3 Enters into the unrelated
    draft. r5: the post-send watermark shows FEWER submitted turns than the
    baseline (eviction) → indeterminate; and even setting that aside, the active
    chip does NOT own our dispatch → never Entered.
    """
    # Baseline: three submitted turns present before the paste.
    presend = (
        "• SEED_OK\n\n"
        "› [Pasted Content 100 chars]\n\n• a\n\n"
        "› [Pasted Content 200 chars]\n\n• b\n\n"
        "› [Pasted Content 300 chars]\n\n• c\n\n"
        "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    baseline = _baseline_from(presend)
    assert baseline.turn_count == 3
    # POST: the tail evicted history down to a single UNRELATED active chip
    # (different N than ours) — watermark shrank from 3 to 0 history turns.
    post = "• previous output scrolled off\n\n› [Pasted Content 555 chars]\n\n" + FOOTER + "\n"
    assert _new_task(post, CHIP_MESSAGE, baseline) == CODEX_SUBMIT_STATE_INDETERMINATE

    provider = _provider()
    backend = _backend_returning(post)
    with pytest.raises(CodexSubmitStuckError):
        provider.verify_submission_after_send(
            METADATA, backend, message=CHIP_MESSAGE, baseline=baseline
        )
    # No blind Enter into the unrelated draft (BLOCKER 3).
    assert _enter_calls(backend) == 0


def test_E1_unrelated_active_chip_is_never_entered_even_without_eviction():
    """An unrelated active chip (wrong N) with an intact window must not be
    Entered: ownership fails → indeterminate → defer, zero Enters."""
    presend = "• SEED_OK\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n"
    baseline = _baseline_from(presend)
    # Active composer carries a chip whose N is NOT ours.
    post = "• SEED_OK\n\n› [Pasted Content 555 chars]\n\n" + FOOTER + "\n"
    provider = _provider()
    backend = _backend_returning(post)
    with pytest.raises(CodexSubmitStuckError):
        provider.verify_submission_after_send(
            METADATA, backend, message=CHIP_MESSAGE, baseline=baseline
        )
    assert _enter_calls(backend) == 0


def test_E1_negative_control_our_own_stuck_chip_is_entered():
    """Negative control: an intact window with OUR chip (matching N) drafted is
    genuinely stuck and MUST drive recovery Enters."""
    presend = "• SEED_OK\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n"
    baseline = _baseline_from(presend)
    post = _pane_with_active_chip("", chars=len(CHIP_MESSAGE))
    provider = _provider()
    backend = _backend_returning(post)
    with pytest.raises(CodexSubmitStuckError):
        provider.verify_submission_after_send(
            METADATA, backend, message=CHIP_MESSAGE, baseline=baseline
        )
    assert _enter_calls(backend) == CODEX_SUBMIT_VERIFY_MAX_RETRIES


def test_E1_negative_control_no_eviction_new_turn_confirms():
    """Negative control: with the window intact and our NEW submitted turn
    present, confirmation succeeds (the watermark did NOT shrink)."""
    presend = (
        "• SEED_OK\n\n"
        "› [Pasted Content 100 chars]\n\n• a\n\n"
        "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    baseline = _baseline_from(presend)
    assert baseline.turn_count == 1
    post = (
        "• SEED_OK\n\n"
        "› [Pasted Content 100 chars]\n\n• a\n\n"
        "› [Pasted Content 3048 chars]\n\n• on it\n\n"  # our NEW turn
        "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    assert _new_task(post, CHIP_MESSAGE, baseline) == CODEX_SUBMIT_STATE_SUBMITTED


# ===========================================================================
# Missing / failed baseline is never success (guards the seam wiring).
# ===========================================================================


def test_missing_baseline_is_indeterminate_never_success():
    """A None baseline (capture_submission_baseline returned None / non-codex)
    forces indeterminate — never a manufactured confirmation."""
    post = (
        "• SEED_OK\n\n› [Pasted Content 3048 chars]\n\n• On it.\n\n"
        "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    assert _new_task(post, CHIP_MESSAGE, None) == CODEX_SUBMIT_STATE_INDETERMINATE


def test_failed_baseline_capture_is_indeterminate_never_success():
    """A baseline whose capture FAILED (captured_ok=False) is never success."""
    failed = CodexProvider._build_submission_baseline(None)
    assert failed.captured_ok is False
    post = (
        "• SEED_OK\n\n› [Pasted Content 3048 chars]\n\n• On it.\n\n"
        "› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    assert _new_task(post, CHIP_MESSAGE, failed) == CODEX_SUBMIT_STATE_INDETERMINATE
