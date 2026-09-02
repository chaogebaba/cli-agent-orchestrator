"""#555: split paste chips are a submittable composer state, and the
delivery readiness gate reads the VIEWPORT, not the scrollback.

Two defects, both observed live on codex-cli 0.152.0 on 2026-09-02.

DEFECT 1 — a long task killed the worker. codex splits a long bracketed paste
into SEVERAL chips: a ~2000-char task rendered as
``› [Pasted Content 1022 chars][Pasted Content 1012 chars]``. Two independent
readers mis-classified that composer:

  (a) ``_active_composer_chip_count`` used a single ``re.search`` and returned
      only the FIRST chip's count (1022). Ownership in ``_pane_shows_stuck_chip``
      compares that against ``len(message)`` (~2034) with ±1 slack, so it failed
      by ~1000 chars, the recovery Enter never fired, and delivery was reported
      "structurally unconfirmed". Journal, terminal 7652d61d, 17:43:01–17:43:04:
      ``no stuck chip visible (attempt 1/3 .. 3/3)``.
  (b) ``read_composer_draft`` returns ``None`` for a non-empty composer sitting
      below assistant output (an ownership-uncertainty guard for HUMAN drafts).
      The stuck pane always has the SEED_OK bullet above the composer, so every
      deferred-init retry read "unreadable" and the worker was torn down.
      Journal, same terminal, 17:43:09 and 17:43:11:
      ``Composer state is unreadable`` ×2 → ``DeliveryDeferredError`` → teardown.

  Scoreboard that day: 5/5 short dispatches delivered, 2/2 long ones died
  (6aee61fb, 7652d61d).

DEFECT 2 — a flat 45 s of dead air before every task paste. ``pre_paste_gate``
polled ``get_history(tail_lines=PYTE_SCREEN_ROWS)``, i.e. 200 rows of
SCROLLBACK. codex renders on the normal screen, so its startup card scrolls up
but stays inside that window — and the card permanently contains
``model:       loading`` / ``Resuming session…``. The banner term of
``_codex_tui_is_ready_for_submit`` therefore never cleared. Measured from the
journal: 41 of 41 codex spawns logged "TUI not ready after 45.0s"; the ready
path never logged once. Live pane 5de4ed8a proved the scoping — 0 banner rows
in its viewport, 2 in its 200-row history, composer and footer plainly live.
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
    _codex_tui_is_ready_for_submit,
    _is_codex_paste_chip_chrome,
)

METADATA_BASE = {"tmux_session": "sess", "tmux_window": "win"}
FOOTER = "  ? for shortcuts                     100% context left"

# The trailer CAO appends to a dispatched task; codex leaves this short tail
# literal after the collapsed chips.
TRAILER = " [Assigned by terminal 5561a7d1]"

# The exact split the operator screenshotted on terminal 7652d61d.
CHIP_A = 1022
CHIP_B = 1012


# ---------------------------------------------------------------------------
# Clock: bounded, so the verify loop's sleeps/deadlines resolve instantly.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bounded_clock():
    state = {"t": 0.0}

    def _monotonic() -> float:
        state["t"] += 0.5
        return state["t"]

    def _sleep(seconds: float) -> None:
        state["t"] += seconds

    with patch("cli_agent_orchestrator.providers.codex.time.monotonic", side_effect=_monotonic):
        with patch("cli_agent_orchestrator.providers.codex.time.sleep", side_effect=_sleep):
            yield state


# ---------------------------------------------------------------------------
# Pane fixtures
# ---------------------------------------------------------------------------


def _pane(composer_body: str) -> str:
    """A live codex pane whose active composer holds ``composer_body``.

    The SEED_OK assistant bullet above the composer is what the real stuck pane
    always shows, and is exactly what used to make ``read_composer_draft``
    return ``None``.
    """
    return f"• SEED_OK\n\n› {composer_body}\n\n{FOOTER}\n"


def _chip(n: int) -> str:
    return f"[Pasted Content {n} chars]"


PANE_SINGLE_CHIP = _pane(_chip(3048))
PANE_TWO_CHIPS = _pane(_chip(CHIP_A) + _chip(CHIP_B))
PANE_TWO_CHIPS_TRAILER = _pane(_chip(CHIP_A) + _chip(CHIP_B) + TRAILER)
PANE_CHIP_TRAILER = _pane(_chip(3048) + TRAILER)
PANE_EMPTY_COMPOSER = _pane("Ask Codex to do anything")


def _provider() -> CodexProvider:
    return CodexProvider("term1234", "sess", "win")


def _metadata(session_uuid: str = "test-uuid-1234") -> dict[str, Any]:
    return {**METADATA_BASE, "provider_session_id": session_uuid}


def _backend_returning(*panes: str) -> MagicMock:
    backend = MagicMock()
    seq = list(panes)

    def _get_history(session, window, tail_lines=None, strip_escapes=False):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    backend.get_history.side_effect = _get_history
    backend.capture_viewport.side_effect = lambda session, window: seq[0]
    return backend


def _enter_calls(backend: MagicMock) -> int:
    return sum(
        1
        for c in backend.send_special_key.call_args_list
        if "Enter" in c.args or c.kwargs.get("key") == "Enter"
    )


@pytest.fixture()
def rollout_dir(tmp_path: Path) -> Path:
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


def _baseline(rollout_path: Path, pre_paste_chip_count: int | None = None) -> CodexSubmitBaseline:
    return CodexSubmitBaseline(
        rollout_path=rollout_path,
        rollout_offset=rollout_path.stat().st_size if rollout_path.exists() else 0,
        captured_ok=True,
        pre_paste_chip_count=pre_paste_chip_count,
    )


# ===========================================================================
# Chip accounting: every chip counts, not just the first
# ===========================================================================


class TestChipAccounting:
    def test_single_chip_counts_itself(self):
        state = CodexProvider._active_composer_paste_state(PANE_SINGLE_CHIP)
        assert state == ([3048], 0)
        assert CodexProvider._active_composer_chip_count(PANE_SINGLE_CHIP) == 3048

    def test_two_chips_sum_not_first_only(self):
        """The regression guard: 1022+1012, never 1022."""
        state = CodexProvider._active_composer_paste_state(PANE_TWO_CHIPS)
        assert state == ([CHIP_A, CHIP_B], 0)
        assert CodexProvider._active_composer_chip_count(PANE_TWO_CHIPS) == CHIP_A + CHIP_B

    def test_two_chips_with_trailer_measure_the_literal_tail(self):
        chips, tail = CodexProvider._active_composer_paste_state(PANE_TWO_CHIPS_TRAILER)
        assert chips == [CHIP_A, CHIP_B]
        assert tail == len(TRAILER.strip())

    def test_single_chip_with_trailer(self):
        chips, tail = CodexProvider._active_composer_paste_state(PANE_CHIP_TRAILER)
        assert chips == [3048]
        assert tail == len(TRAILER.strip())

    def test_empty_composer_has_no_chips(self):
        chips, tail = CodexProvider._active_composer_paste_state(PANE_EMPTY_COMPOSER)
        assert chips == []
        assert CodexProvider._active_composer_chip_count(PANE_EMPTY_COMPOSER) is None

    def test_unanchorable_capture_returns_none(self):
        assert CodexProvider._active_composer_paste_state("") is None
        assert CodexProvider._active_composer_chip_count("") is None

    def test_historical_chip_is_not_the_active_composer(self):
        """A submitted chip that scrolled into history must not be counted."""
        pane = (
            "• SEED_OK\n\n"
            f"› {_chip(CHIP_A)}\n\n"
            "• On it.\n\n"
            "› Ask Codex to do anything\n\n"
            f"{FOOTER}\n"
        )
        chips, _tail = CodexProvider._active_composer_paste_state(pane)
        assert chips == []


# ===========================================================================
# Recovery Enter fires for a SPLIT paste (the worker-killing defect)
# ===========================================================================


class TestSplitPasteRecovery:
    """A composer showing several chips is stuck-and-ours → re-Enter it.

    ``verify_submission_after_send`` still raises ``CodexSubmitStuckError``
    here, because the fake rollout never gains the user event — that is the
    correct B1 invariant (an Enter is an ACTION, never a confirmation). What
    these assert is that the recovery Enter FIRED at all, which is precisely
    what the first-chip-only reader suppressed.
    """

    def test_two_chips_summing_to_dispatch_are_owned(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        msg = "y" * (CHIP_A + CHIP_B)
        provider = _provider()
        backend = _backend_returning(PANE_TWO_CHIPS)

        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=_baseline(rollout_dir)
            )
        assert _enter_calls(backend) == CODEX_SUBMIT_VERIFY_MAX_RETRIES

    def test_two_chips_plus_trailer_are_owned(self, rollout_dir: Path, patched_codex_home: Path):
        msg = "y" * (CHIP_A + CHIP_B) + TRAILER
        provider = _provider()
        backend = _backend_returning(PANE_TWO_CHIPS_TRAILER)

        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=_baseline(rollout_dir)
            )
        assert _enter_calls(backend) == CODEX_SUBMIT_VERIFY_MAX_RETRIES

    def test_single_chip_still_recovers(self, rollout_dir: Path, patched_codex_home: Path):
        """The pre-existing F435 stuck-chip case is untouched."""
        msg = "y" * 3048
        provider = _provider()
        backend = _backend_returning(PANE_SINGLE_CHIP)

        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=_baseline(rollout_dir)
            )
        assert _enter_calls(backend) == CODEX_SUBMIT_VERIFY_MAX_RETRIES

    def test_chips_far_larger_than_dispatch_are_not_owned(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """Ownership is still checked: an unrelated, much larger paste is not ours."""
        msg = "y" * 40
        provider = _provider()
        backend = _backend_returning(PANE_TWO_CHIPS)

        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=msg, baseline=_baseline(rollout_dir)
            )
        assert _enter_calls(backend) == 0

    def test_ambiguous_prepaste_chip_still_defers(
        self, rollout_dir: Path, patched_codex_home: Path
    ):
        """B2 r7 is preserved for the split case: a same-total pre-paste draft
        makes ownership unresolvable, so we defer rather than blind-Enter."""
        from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError

        msg = "y" * (CHIP_A + CHIP_B)
        provider = _provider()
        backend = _backend_returning(PANE_TWO_CHIPS)

        with pytest.raises(DeliveryDeferredError, match="unresolvable"):
            provider.verify_submission_after_send(
                _metadata(),
                backend,
                message=msg,
                baseline=_baseline(rollout_dir, pre_paste_chip_count=CHIP_A + CHIP_B),
            )
        assert _enter_calls(backend) == 0


# ===========================================================================
# The composer reader: chips are READABLE, not "unreadable"
# ===========================================================================


class TestChipComposerIsReadable:
    def test_chip_chrome_predicate(self):
        assert _is_codex_paste_chip_chrome(_chip(CHIP_A)) is True
        assert _is_codex_paste_chip_chrome(_chip(CHIP_A) + _chip(CHIP_B)) is True
        assert _is_codex_paste_chip_chrome(_chip(CHIP_A) + TRAILER) is True
        assert _is_codex_paste_chip_chrome("") is False
        assert _is_codex_paste_chip_chrome("Ask Codex to do anything") is False
        # Human prose that merely mentions a chip further along is NOT chrome.
        assert _is_codex_paste_chip_chrome(f"please resend the {_chip(12)}") is False

    def test_chips_below_assistant_output_are_read_not_deferred(self):
        """The teardown bug: this used to return None → "Composer state is
        unreadable" → deferred init killed the worker."""
        provider = _provider()
        draft = provider.read_composer_draft(PANE_TWO_CHIPS.splitlines())
        assert draft is not None
        assert _is_codex_paste_chip_chrome(draft)

    def test_single_chip_below_assistant_output_is_read(self):
        provider = _provider()
        draft = provider.read_composer_draft(PANE_SINGLE_CHIP.splitlines())
        assert draft is not None
        assert _chip(3048) in draft

    def test_human_prose_below_assistant_output_still_defers(self):
        """The ownership guard is intact for anything that is NOT chip chrome."""
        provider = _provider()
        pane = _pane("wait, hold off on that refactor")
        assert provider.read_composer_draft(pane.splitlines()) is None

    def test_provider_declares_its_own_paste_chrome(self):
        provider = _provider()
        assert provider.draft_is_own_paste_chrome(_chip(CHIP_A) + _chip(CHIP_B)) is True
        assert provider.draft_is_own_paste_chrome("hold off on that refactor") is False


class TestDraftGuardClearsOwnChrome:
    """Our own unsubmitted paste is cleared, never stashed and restored."""

    def _run(self, draft: str):
        from cli_agent_orchestrator.services import draft_guard

        provider = _provider()
        with patch.object(draft_guard, "_read_provider_draft", return_value=draft):
            with patch.object(draft_guard, "_wait_for_stable_draft", return_value=draft):
                with patch.object(draft_guard, "_clear_composer", return_value=True) as clear:
                    with patch.object(draft_guard, "_append_draft_log") as log:
                        with patch.object(draft_guard, "_consult_dialog_before_send"):
                            with patch.object(
                                draft_guard, "_clear_step_changed_draft", return_value=True
                            ):
                                result = draft_guard.preserve_draft_before_send(
                                    "term1234", dict(METADATA_BASE), provider
                                )
        return result, clear, log

    def test_chip_chrome_is_cleared_and_not_preserved(self):
        result, clear, log = self._run(_chip(CHIP_A) + _chip(CHIP_B))
        assert result is None, "chip chrome must never come back as a PreservedDraft"
        assert clear.called, "chip chrome must still be CLEARED before the retry paste"
        assert not log.called, "our own paste is not a human draft; do not log it"

    def test_human_draft_is_still_preserved(self):
        result, _clear, log = self._run("wait, hold off on that refactor")
        assert result is not None
        assert result.text == "wait, hold off on that refactor"
        assert log.called


# ===========================================================================
# Readiness gate: read the viewport, not the scrollback
# ===========================================================================

# The codex startup card, as it sits in scrollback for the whole init-and-
# deliver phase. Byte-shape taken from live pane 5de4ed8a.
STARTUP_CARD_IN_SCROLLBACK = (
    "╭────────────────────────────────────────────╮\n"
    "│ >_ OpenAI Codex (v0.152.0)                 │\n"
    "│                                            │\n"
    "│ model:       loading   /model to change    │\n"
    "│ directory:   ~/cli-subagents               │\n"
    "╰────────────────────────────────────────────╯\n"
    "  Resuming session…\n"
    "\n"
)
LIVE_VIEWPORT_READY = "• SEED_OK\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n"
# What the gate used to see: the card is 200-row history, so the banner is
# still "on screen" as far as a whole-capture search is concerned.
HISTORY_WITH_CARD = STARTUP_CARD_IN_SCROLLBACK + LIVE_VIEWPORT_READY
# A genuinely initializing TUI: the card IS the visible screen, no footer yet.
VIEWPORT_STILL_LOADING = STARTUP_CARD_IN_SCROLLBACK


class TestReadinessGateReadsViewport:
    def _backend(self, viewport: str, history: str) -> MagicMock:
        backend = MagicMock()
        backend.capture_viewport.side_effect = lambda session, window: viewport
        backend.get_history.side_effect = lambda *a, **k: history
        return backend

    def test_ready_viewport_with_card_in_scrollback_does_not_wait(self, caplog):
        """The 45 s gap, gone. The card in scrollback must not hold the gate."""
        import logging

        provider = _provider()
        backend = self._backend(LIVE_VIEWPORT_READY, HISTORY_WITH_CARD)
        with patch("cli_agent_orchestrator.providers.codex.get_backend", return_value=backend):
            with caplog.at_level(logging.WARNING):
                provider.pre_paste_gate()

        assert backend.capture_viewport.call_count == 1, "ready ⇒ exactly one read"
        assert not backend.get_history.called, (
            "the gate must never consult scrollback: the startup card lives there "
            "and its banner never clears"
        )
        assert "not ready" not in caplog.text

    def test_history_capture_would_still_look_not_ready(self):
        """Pins the defect itself, so a revert to get_history cannot pass silently."""
        assert _codex_tui_is_ready_for_submit(HISTORY_WITH_CARD) is False
        assert _codex_tui_is_ready_for_submit(LIVE_VIEWPORT_READY) is True

    def test_loading_viewport_still_waits_then_proceeds(self):
        """The gate is not weakened: a card ON SCREEN still blocks."""
        provider = _provider()
        backend = MagicMock()
        seq = [VIEWPORT_STILL_LOADING, VIEWPORT_STILL_LOADING, LIVE_VIEWPORT_READY]
        backend.capture_viewport.side_effect = lambda session, window: (
            seq.pop(0) if len(seq) > 1 else seq[0]
        )
        with patch("cli_agent_orchestrator.providers.codex.get_backend", return_value=backend):
            provider.pre_paste_gate()
        assert backend.capture_viewport.call_count >= 3

    def test_viewport_read_failure_proceeds_without_raising(self):
        provider = _provider()
        backend = MagicMock()
        backend.capture_viewport.side_effect = RuntimeError("pane read boom")
        with patch("cli_agent_orchestrator.providers.codex.get_backend", return_value=backend):
            provider.pre_paste_gate()  # must swallow and return

    def test_gate_never_sends_keys(self):
        provider = _provider()
        backend = self._backend(LIVE_VIEWPORT_READY, HISTORY_WITH_CARD)
        with patch("cli_agent_orchestrator.providers.codex.get_backend", return_value=backend):
            provider.pre_paste_gate()
        assert not backend.send_special_key.called
        assert not backend.send_keys.called


class TestReadinessGapIsGone:
    """Timing, measured on the gate's own clock rather than wall time.

    ``_bounded_clock`` advances ``time.monotonic`` by 0.5 s per call and by the
    slept duration, so the gate's own accounting of elapsed time is observable
    and deterministic.
    """

    def test_ready_pane_consumes_no_readiness_budget(self, _bounded_clock):
        provider = _provider()
        backend = MagicMock()
        backend.capture_viewport.side_effect = lambda session, window: LIVE_VIEWPORT_READY
        with patch("cli_agent_orchestrator.providers.codex.get_backend", return_value=backend):
            start = _bounded_clock["t"]
            provider.pre_paste_gate()
            elapsed = _bounded_clock["t"] - start
        # One deadline read plus one viewport read; no poll sleep at all. The
        # pre-fix path burned the full 45.0 s budget here on 41/41 spawns.
        assert elapsed < 1.0
