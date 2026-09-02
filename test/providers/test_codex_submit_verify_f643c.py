"""F643c (#498): delivery readiness gate + composer-holds-draft recovery Enter.

Third-order follow-up to F643 / F643b. The instrumented live probe (terminal
6351a149, codex-cli 0.151.0) showed the REAL delivery-side failure: CAO pastes
the first task while the resume TUI is still initializing — the startup card
renders ``model:       loading`` / ``Resuming session…`` (a trust-directory
dialog may interpose) — and the submit Enter is CONSUMED/DROPPED during that
window. After init completes the task text sits UNSUBMITTED in the composer, and
F435's recovery loop only ever recovered the stuck-paste-CHIP shape: when it saw
"no stuck chip visible" it just re-checked the rollout and never re-sent Enter,
so delivery died "structurally unconfirmed" and the healthy terminal was town
down.

Two legs, both modelled here with fake screen fixtures (no tmux, no real codex):

(a) READINESS GATE — ``CodexProvider.pre_paste_gate`` blocks (bounded) until the
    TUI footer is present with no loading/resuming banner, then proceeds. It is
    a NO-OP on a warm pane and WARN-and-proceeds (never raises) if readiness
    never resolves — a gate must never permanently withhold a delivery.

(b) COMPOSER-HOLDS-DRAFT RECOVERY — inside the existing 3-attempt recovery loop,
    when NO chip is visible AND ``read_composer_draft`` shows the composer still
    holding the delivered task text, re-send Enter (bounded), then re-verify via
    the rollout. Re-sending Enter is an ACTION, never a confirmation (B1
    invariant preserved): only a positive rollout/SQLite match returns success.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.codex import (
    CODEX_DELIVERY_READINESS_TIMEOUT_SECONDS,
    CodexProvider,
    CodexSubmitBaseline,
    CodexSubmitStuckError,
    _codex_tui_is_ready_for_submit,
)

METADATA_BASE = {"tmux_session": "sess", "tmux_window": "win"}
# A codex TUI status/footer line the startup-readiness predicate AND the
# composer-draft reader both recognize ("? for shortcuts" + context-left).
FOOTER = "  ? for shortcuts                     100% context left"
SESSION_UUID = "01a05508-9adc-73e0-a0bb-5c0da078415c"

# The message must be long enough to form a distinctive echo signature
# (CODEX_SUBMIT_TASK_SIGNATURE_MIN_CHARS = 12).
MSG = "Root-cause and fix F643c per the assignment [callback: terminal 9064394e]"


# ---------------------------------------------------------------------------
# Fake screen fixtures
# ---------------------------------------------------------------------------

# The codex 0.151.0 startup card while the model is still resolving. Byte-shape
# matched to test/auto_answers/fixtures/f530/05-trust-dir-startup-card-15a6fa21.txt.
LOADING_BANNER_PANE = (
    "  Do you trust the contents of this directory?\n"
    "\n"
    "› 1. Yes, continue\n"
    "  2. No, quit\n"
    "\n"
    "  Press enter to continue\n"
    "╭──────────────────────────────────────────────────────────╮\n"
    "│ >_ OpenAI Codex (v0.151.0)                               │\n"
    "│                                                          │\n"
    "│ model:       loading   /model to change                  │\n"
    "│ directory:   ~/VScode_projects/…/.cao/worktrees/0436cf96 │\n"
    "│ permissions: YOLO mode                                   │\n"
    "╰──────────────────────────────────────────────────────────╯\n"
)

# Transient resume banner (the model row already resolved but replay is live).
RESUMING_BANNER_PANE = (
    "  Resuming session…\n"
    "\n"
    "› Ask Codex to do anything\n"
    "\n"
    f"{FOOTER}\n"
)

# The TUI is live: idle composer placeholder + footer, no loading/resuming banner.
READY_IDLE_PANE = "\n\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n"


def _composer_holds_draft_pane(text: str) -> str:
    """A live composer holding the delivered task as an unsubmitted DRAFT.

    The dropped submit Enter left the raw task text in the composer (no collapsed
    paste chip). Rendered as ``› <text>`` at the bottom above the footer, which
    is the shape ``read_composer_draft`` parses. No assistant/spinner line sits
    directly above the composer (that shape makes the reader return ``None`` —
    an intentional ownership-uncertainty guard), matching a freshly resumed pane.
    """
    return "\n\n\n" f"› {text}\n\n" f"{FOOTER}\n"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("cli_agent_orchestrator.providers.codex.time.sleep", return_value=None):
        yield


@pytest.fixture()
def _fast_monotonic():
    """A monotonic clock that advances slowly (0.3s/call) so bounded readiness
    loops iterate several times before the 12s deadline, then terminate."""
    counter = {"t": 0.0}

    def _mono():
        counter["t"] += 0.3
        return counter["t"]

    with patch("cli_agent_orchestrator.providers.codex.time.monotonic", side_effect=_mono):
        yield


def _provider() -> CodexProvider:
    return CodexProvider("term1234", "sess", "win")


def _metadata(session_uuid: str = SESSION_UUID) -> dict[str, Any]:
    return {**METADATA_BASE, "provider_session_id": session_uuid}


def _backend_returning(*panes: str) -> MagicMock:
    """Mock backend serving panes in sequence (last repeats).

    Both capture seams draw from the same queue. #555 moved the
    readiness gate off ``get_history`` — 200 rows of scrollback, where the codex
    startup card's ``model: loading`` / ``Resuming session…`` banner survives
    forever — and onto ``capture_viewport``, the live screen the readiness
    predicate was always meant to judge. The verify/recovery legs below still
    read history, which is correct: they need the submitted-turn transcript.
    """
    backend = MagicMock()
    seq = list(panes)

    def _next(*_args, **_kwargs):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    backend.get_history.side_effect = _next
    backend.capture_viewport.side_effect = _next
    return backend


def _enter_calls(backend: MagicMock) -> int:
    return sum(
        1
        for c in backend.send_special_key.call_args_list
        if "Enter" in c.args or c.kwargs.get("key") == "Enter"
    )


def _baseline(*, captured_ok: bool = True) -> CodexSubmitBaseline:
    # No rollout infra pinned; verify degrades to the pane/recovery path, which
    # is exactly the F643c leg under test.
    return CodexSubmitBaseline(
        rollout_path=None,
        rollout_offset=0,
        baseline_wall=time.time(),
        captured_ok=captured_ok,
    )


# ===========================================================================
# Pure predicate: _codex_tui_is_ready_for_submit
# ===========================================================================


class TestReadinessPredicate:
    def test_loading_banner_is_not_ready(self):
        assert _codex_tui_is_ready_for_submit(LOADING_BANNER_PANE) is False

    def test_resuming_banner_is_not_ready(self):
        assert _codex_tui_is_ready_for_submit(RESUMING_BANNER_PANE) is False

    def test_ready_idle_pane_is_ready(self):
        assert _codex_tui_is_ready_for_submit(READY_IDLE_PANE) is True

    def test_composer_holding_draft_is_ready(self):
        # A live composer (footer present, no banner) is READY even if it holds
        # a draft — readiness is about the TUI being past init, not empty.
        assert _codex_tui_is_ready_for_submit(_composer_holds_draft_pane(MSG)) is True

    def test_empty_pane_is_not_ready(self):
        # No footer at all → not ready (nothing rendered yet).
        assert _codex_tui_is_ready_for_submit("") is False
        assert _codex_tui_is_ready_for_submit("\n\n") is False

    def test_loading_wins_even_with_footer(self):
        # A stray footer line alongside a live loading banner must NOT read ready.
        pane = "│ model:       loading   /model to change │\n" + FOOTER + "\n"
        assert _codex_tui_is_ready_for_submit(pane) is False


# ===========================================================================
# Leg (a): pre_paste_gate readiness gate
# ===========================================================================


class TestPrePasteGate:
    def test_ready_pane_returns_immediately_no_wait(self, _fast_monotonic):
        provider = _provider()
        backend = _backend_returning(READY_IDLE_PANE)
        with patch(
            "cli_agent_orchestrator.providers.codex.get_backend", return_value=backend
        ):
            provider.pre_paste_gate()
        # A ready pane is read at least once; the gate never sends keys.
        assert backend.capture_viewport.called
        assert _enter_calls(backend) == 0

    def test_waits_through_loading_then_proceeds_when_ready(self, _fast_monotonic):
        provider = _provider()
        # First read: loading banner. Second: resuming. Third+: ready.
        backend = _backend_returning(
            LOADING_BANNER_PANE, RESUMING_BANNER_PANE, READY_IDLE_PANE
        )
        with patch(
            "cli_agent_orchestrator.providers.codex.get_backend", return_value=backend
        ):
            provider.pre_paste_gate()
        # It re-read the pane until ready (more than one capture).
        assert backend.capture_viewport.call_count >= 3

    def test_never_ready_warn_and_proceeds_bounded(self, _fast_monotonic, caplog):
        import logging

        provider = _provider()
        # Loading forever — the gate must give up (bounded) and proceed, NOT hang
        # and NOT raise.
        backend = _backend_returning(LOADING_BANNER_PANE)
        with patch(
            "cli_agent_orchestrator.providers.codex.get_backend", return_value=backend
        ):
            with caplog.at_level(logging.WARNING):
                provider.pre_paste_gate()  # must return, not raise
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "F643c readiness gate" in joined
        assert "proceeding anyway" in joined

    def test_capture_failure_proceeds_without_raising(self, _fast_monotonic):
        provider = _provider()
        backend = MagicMock()
        backend.get_history.side_effect = RuntimeError("pane read boom")
        with patch(
            "cli_agent_orchestrator.providers.codex.get_backend", return_value=backend
        ):
            provider.pre_paste_gate()  # must swallow and return

    def test_gate_never_raises_is_pure_readiness_not_confirmation(self, _fast_monotonic):
        # B1 invariant guard: the gate is a delivery-side readiness check. It
        # must not emit Enter (that would be an unverified submit action).
        provider = _provider()
        backend = _backend_returning(READY_IDLE_PANE)
        with patch(
            "cli_agent_orchestrator.providers.codex.get_backend", return_value=backend
        ):
            provider.pre_paste_gate()
        backend.send_special_key.assert_not_called()
        backend.send_keys.assert_not_called()


# ===========================================================================
# Leg (b): composer-holds-draft recovery Enter
# ===========================================================================


class TestComposerHoldsDraftRecovery:
    @pytest.fixture(autouse=True)
    def _bounded_monotonic(self):
        """Bound the pre-loop rollout poll: advance monotonic 0.3s/call so the
        12s poll window is exhausted in a few dozen iterations (sleep is a no-op
        via the module-level ``_no_sleep``), keeping each test fast and its RSS
        flat."""
        counter = {"t": 0.0}

        def _mono():
            counter["t"] += 0.3
            return counter["t"]

        with patch(
            "cli_agent_orchestrator.providers.codex.time.monotonic", side_effect=_mono
        ):
            yield

    def test_composer_holds_draft_resends_enter(self):
        """The F643c core: no chip, composer holds our task, rollout negative →
        re-send Enter. Delivery stays unconfirmed (no rollout infra) so it still
        raises — but the recovery Enter MUST have fired."""
        provider = _provider()
        backend = _backend_returning(_composer_holds_draft_pane(MSG))
        with pytest.raises(CodexSubmitStuckError, match="structurally unconfirmed"):
            provider.verify_submission_after_send(
                _metadata(session_uuid=""), backend, message=MSG, baseline=_baseline()
            )
        # The recovery Enter fired at least once (bounded by MAX_RETRIES).
        assert _enter_calls(backend) >= 1

    def test_no_draft_no_chip_does_not_resend_enter(self):
        """Idle composer (placeholder, no draft, no chip): nothing to recover →
        NO Enter (the classic no-chip re-check-only path)."""
        provider = _provider()
        backend = _backend_returning(READY_IDLE_PANE)
        with pytest.raises(CodexSubmitStuckError, match="structurally unconfirmed"):
            provider.verify_submission_after_send(
                _metadata(session_uuid=""), backend, message=MSG, baseline=_baseline()
            )
        assert _enter_calls(backend) == 0

    def test_unrelated_draft_does_not_resend_enter(self):
        """A composer holding a DIFFERENT draft (not our task) must NOT trigger a
        recovery Enter — match is by content signature, never by mere presence."""
        provider = _provider()
        backend = _backend_returning(
            _composer_holds_draft_pane("a completely unrelated note the human typed")
        )
        with pytest.raises(CodexSubmitStuckError, match="structurally unconfirmed"):
            provider.verify_submission_after_send(
                _metadata(session_uuid=""), backend, message=MSG, baseline=_baseline()
            )
        assert _enter_calls(backend) == 0

    def test_recovery_enter_confirmed_by_rollout_returns(self, tmp_path):
        """After the recovery Enter, if the rollout now confirms, verify returns
        success (no raise). Models the submit landing on the re-sent Enter."""
        from pathlib import Path

        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("", encoding="utf-8")

        provider = _provider()
        backend = _backend_returning(_composer_holds_draft_pane(MSG))
        baseline = CodexSubmitBaseline(
            rollout_path=rollout,
            rollout_offset=0,
            baseline_wall=time.time(),
            captured_ok=True,
        )

        calls = {"n": 0}

        def _confirm(rp, offset, msg):
            # Negative until the recovery Enter has fired, then positive.
            if _enter_calls(backend) >= 1:
                return True
            return False

        # Force the JSONL user-event probe to reflect "confirmed after Enter"
        # and keep the other substrates negative.
        with patch.object(provider, "_rollout_has_user_event", side_effect=_confirm), \
             patch.object(provider, "_forked_rollout_match", return_value=False), \
             patch.object(provider, "_sqlite_has_user_event", return_value=False):
            provider.verify_submission_after_send(
                _metadata(), backend, message=MSG, baseline=baseline
            )
        assert _enter_calls(backend) >= 1

    def test_short_message_forms_no_signature_no_false_enter(self):
        """A message too short for a distinctive signature must not blind-Enter a
        composer (guards against false recovery on ambiguous short text)."""
        provider = _provider()
        short = "hi"
        backend = _backend_returning(_composer_holds_draft_pane(short))
        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(session_uuid=""), backend, message=short, baseline=_baseline()
            )
        assert _enter_calls(backend) == 0
