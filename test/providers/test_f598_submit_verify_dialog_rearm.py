"""F598 #455: F435 submit-verify must RE-ARM across a blocking-dialog window.

Incident 55e84b8a (root cause, journal-confirmed): a task paste landed while the
codex trust-dir → resume-workdir-chooser sequence owned the pane, so the submit
Enter was absorbed and the `[Pasted Content 3340 chars]` chip stayed drafted. The
journal shows exactly one F435 line —

    F435/F491 submit-verify: terminal 55e84b8a has active dialog
    (status WAITING_USER_ANSWER); cannot recover with Enter

— i.e. F435 ran ONCE, hit the F491 pre-check while the dialog was still up, raised
CodexSubmitStuckError, and GAVE UP. It never re-verified once the dialog cleared
~4.5 min later and the composer finally rendered, so the chip was never submitted
until a human pressed Enter.

The fix RE-ARMS: while the dialog blocks, F435 repeatedly nudges the auto-responder
and waits (bounded) for the WHOLE sequence to clear, then proceeds to the normal
composer stuck-chip recovery Enter. These tests reproduce that exact frame
sequence and prove recovery (and that a never-clearing dialog still raises).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import (
    CODEX_SUBMIT_VERIFY_DIALOG_REARM_ATTEMPTS,
    CodexProvider,
    CodexSubmitBaseline,
    CodexSubmitStuckError,
)

MESSAGE = "x" * 3340  # the 55e84b8a task (chip reads [Pasted Content 3340 chars])
FOOTER = (
    "  ~/VScode_projects/cli-subagents/cli-agent-orchestrator/.cao/worktrees/55e84b8a "
    "· cao/55e84b8a · gpt-5.6-sol high"
)

# Frame while the resume-workdir chooser owns the pane (the chip is drafted ABOVE
# it, not in an active composer).
CHOOSER_FRAME = (
    "› [Pasted Content 3340 chars]\n\n"
    "Choose working directory to resume this session\n\n"
    "  1. Use session directory\n"
    "› 2. Use current directory\n"
    "  Press enter to continue\n"
)
# Frame after the dialog cleared: the composer is live and STILL shows the chip.
COMPOSER_STUCK_FRAME = "• Hello! 👋\n\n" f"› [Pasted Content 3340 chars]\n\n{FOOTER}\n"


def _provider() -> CodexProvider:
    return CodexProvider("term1234", "sess", "win")


def _metadata() -> dict[str, Any]:
    return {"tmux_session": "sess", "tmux_window": "win", "provider_session_id": "uuid-55e84b8a"}


def _baseline() -> CodexSubmitBaseline:
    # No pre-paste draft chip → ownership is unambiguous.
    return CodexProvider._build_submission_baseline("• SEED_OK\n\n› Ask Codex to do anything\n")


@pytest.fixture()
def rollout(tmp_path: Path):
    d = tmp_path / "sessions" / "uuid-55e84b8a"
    d.mkdir(parents=True)
    rp = d / "rollout-uuid-55e84b8a.jsonl"
    rp.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "uuid-55e84b8a"}}) + "\n",
        encoding="utf-8",
    )
    return rp


def _write_user_event(rp: Path, message: str) -> None:
    with rp.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"type": "event_msg", "payload": {"type": "user_message", "message": message}}
            )
            + "\n"
        )


def _enter_calls(backend: MagicMock) -> int:
    return sum(
        1
        for c in backend.send_special_key.call_args_list
        if "Enter" in c.args or c.kwargs.get("key") == "Enter"
    )


def test_rearm_recovers_after_dialog_sequence_clears(rollout, monkeypatch):
    """The 55e84b8a sequence: dialog blocks the verify window, then clears; F435
    re-arms, sends the composer recovery Enter, and the rollout then confirms."""
    provider = _provider()
    provider._resolve_rollout_file = lambda _uuid: rollout  # type: ignore[assignment]

    # Pane: chooser while blocked, composer+chip once cleared.
    frames = {"v": CHOOSER_FRAME}
    backend = MagicMock()
    backend.get_history.side_effect = lambda *a, **k: frames["v"]

    # Status: WAITING for the first few re-arm cycles, then clears.
    status_seq = [
        TerminalStatus.WAITING_USER_ANSWER,
        TerminalStatus.WAITING_USER_ANSWER,
        TerminalStatus.PROCESSING,  # dialog cleared on the 3rd recheck
    ]

    def _get_status(_tid):
        return status_seq.pop(0) if len(status_seq) > 1 else status_seq[0]

    # When the dialog clears, the pane becomes the composer-with-chip; the
    # recovery Enter then "submits" → write the rollout user event.
    def _clear_pane_to_composer():
        frames["v"] = COMPOSER_STUCK_FRAME

    def _send_special_key(_s, _w, key):
        if key == "Enter":
            _write_user_event(rollout, MESSAGE)

    backend.send_special_key.side_effect = _send_special_key

    fake_sm = MagicMock()
    fake_sm.get_status.side_effect = _get_status
    fake_sm.get_rendered_screen.return_value = CHOOSER_FRAME.splitlines()
    fake_ar = MagicMock()

    # When the auto-responder is nudged the 3rd time, model the dialog clearing.
    nudges = {"n": 0}

    def _on_screen(_tid, _prov, _lines):
        nudges["n"] += 1
        if nudges["n"] >= 2:
            _clear_pane_to_composer()

    fake_ar.on_screen.side_effect = _on_screen

    with (
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor", fake_sm),
        patch("cli_agent_orchestrator.services.auto_responder.auto_responder", fake_ar),
        patch("cli_agent_orchestrator.providers.codex.time.sleep", lambda _s: None),
        patch(
            "cli_agent_orchestrator.providers.codex.time.monotonic",
            side_effect=[float(i) for i in range(0, 2000)],
        ),
    ):
        # Must NOT raise: the chip is recovered once the dialog clears.
        provider.verify_submission_after_send(
            _metadata(), backend, message=MESSAGE, baseline=_baseline()
        )

    # The composer recovery Enter fired at least once after the dialog cleared.
    assert _enter_calls(backend) >= 1


def test_never_clearing_dialog_still_raises(rollout, monkeypatch):
    """Guard: if the dialog NEVER clears within the re-arm budget, F435 still
    raises CodexSubmitStuckError (defer → redeliver), not a false success."""
    provider = _provider()
    provider._resolve_rollout_file = lambda _uuid: rollout  # type: ignore[assignment]

    backend = MagicMock()
    backend.get_history.side_effect = lambda *a, **k: CHOOSER_FRAME

    fake_sm = MagicMock()
    fake_sm.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER  # never clears
    fake_sm.get_rendered_screen.return_value = CHOOSER_FRAME.splitlines()
    fake_ar = MagicMock()

    with (
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor", fake_sm),
        patch("cli_agent_orchestrator.services.auto_responder.auto_responder", fake_ar),
        patch("cli_agent_orchestrator.providers.codex.time.sleep", lambda _s: None),
        patch(
            "cli_agent_orchestrator.providers.codex.time.monotonic",
            side_effect=[float(i) for i in range(0, 2000)],
        ),
    ):
        with pytest.raises(CodexSubmitStuckError):
            provider.verify_submission_after_send(
                _metadata(), backend, message=MESSAGE, baseline=_baseline()
            )

    # Re-armed the full budget before giving up (nudged the auto-responder each cycle).
    assert fake_ar.on_screen.call_count == CODEX_SUBMIT_VERIFY_DIALOG_REARM_ATTEMPTS
