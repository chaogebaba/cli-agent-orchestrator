"""F435: codex paste-submit race recovery (verify_submission_after_send).

Symptom: dispatching a task to a codex TUI worker pastes the message into the
composer, but the submit Enter is sometimes lost under concurrent multi-assign
— the pane sits at ``› [Pasted Content NNNN chars]`` until the stalled-callback
watchdog fires (~120s). Fix: after the send, verify the paste submitted; while
the stuck chip is present, re-send Enter with bounded retries; raise a clear
error if it never submits. Idempotent — never blind-Enter a submitted composer.

These tests drive the provider's real ``verify_submission_after_send`` entry
point with a mocked tmux backend and assert the observable Enter re-sends.
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

METADATA = {"tmux_session": "sess", "tmux_window": "win"}


@pytest.fixture(autouse=True)
def _no_sleep():
    """Neutralize the grace/backoff sleeps so retries run instantly."""
    with patch("cli_agent_orchestrator.providers.codex.time.sleep", return_value=None):
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


def test_chip_regex_matches_various_char_counts_and_glyphs():
    assert CodexProvider._pane_shows_pasted_chip("› [Pasted Content 12 chars]") is True
    assert CodexProvider._pane_shows_pasted_chip("» [Pasted Content 999999 chars]") is True


# --- submitted immediately → no extra Enter -------------------------------


def test_submitted_immediately_sends_no_extra_enter():
    provider = _provider()
    backend = _backend_returning(SUBMITTED_PLACEHOLDER_PANE)

    provider.verify_submission_after_send(METADATA, backend)

    backend.send_special_key.assert_not_called()


def test_submitted_working_spinner_sends_no_extra_enter():
    provider = _provider()
    backend = _backend_returning(SUBMITTED_WORKING_PANE)

    provider.verify_submission_after_send(METADATA, backend)

    backend.send_special_key.assert_not_called()


# --- stuck then recovered → exactly the Enters needed ---------------------


def test_stuck_then_recovered_after_one_reenter():
    provider = _provider()
    # grace-check sees stuck; after one re-Enter the re-verify sees submitted.
    backend = _backend_returning(STUCK_CHIP_PANE, SUBMITTED_PLACEHOLDER_PANE)

    provider.verify_submission_after_send(METADATA, backend)

    assert _enter_calls(backend) == 1


def test_stuck_then_recovered_after_two_reenters():
    provider = _provider()
    backend = _backend_returning(
        STUCK_CHIP_PANE,  # initial grace check
        STUCK_CHIP_PANE,  # re-verify after 1st Enter: still stuck
        SUBMITTED_WORKING_PANE,  # re-verify after 2nd Enter: submitted
    )

    provider.verify_submission_after_send(METADATA, backend)

    assert _enter_calls(backend) == 2


# --- stuck forever → clear error naming the terminal ----------------------


def test_stuck_forever_raises_after_bounded_retries():
    provider = _provider()
    backend = _backend_returning(STUCK_CHIP_PANE)  # always stuck

    with pytest.raises(CodexSubmitStuckError) as excinfo:
        provider.verify_submission_after_send(METADATA, backend)

    # Bounded: exactly MAX_RETRIES re-Enters, no more.
    assert _enter_calls(backend) == CODEX_SUBMIT_VERIFY_MAX_RETRIES
    assert "term1234" in str(excinfo.value)


# --- capture failure must not blind-Enter (idempotence safety) ------------


def test_capture_failure_is_treated_as_not_stuck():
    """If the pane cannot be captured, we must NOT re-Enter (could double-submit)."""
    provider = _provider()
    backend = MagicMock()
    backend.get_history.side_effect = RuntimeError("tmux gone")

    provider.verify_submission_after_send(METADATA, backend)

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
