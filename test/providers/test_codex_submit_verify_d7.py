"""D7 — the confirm ladder is not entered for a source-authoritative terminal.

With the codex rollout source healthy there is a structural answer to "did this
submit land", and the pane adds nothing to it.  The ladder below the poll exists
to read the pane and decide whether a keystroke is owed, and #555 is what that
costs when the pane cannot be read: the incident's death was not the readiness
gate, which proceeds anyway, but the composer read after it raising on an
unreadable pane.

So the poll gains a cheaper primary signal — the ``submission.confirmed`` row the
rollout tailer already writes — and its exhaustion becomes a DEFERRAL rather than
an excursion into pane archaeology.  ``DeliveryDeferredError`` is deliberately the
exception the two stuck arms in ``terminal_service`` already raise, so the
vocabulary its callers see does not change.

Every test here has an OFF arm, because the gate is the cutover's own predicate:
with ``CAO_WORKER_TRUTH_STATUS`` unset the ladder runs exactly as it does today.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import CodexProvider, CodexSubmitBaseline
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError

MESSAGE = "y" * 120
FOOTER = "  ~/work · main · gpt-5.6-sol high"
#: The composer still holding our chip: the frame the ladder would act on.
STUCK_FRAME = f"• Hello\n\n› [Pasted Content {len(MESSAGE)} chars]\n\n{FOOTER}\n"


def _provider() -> CodexProvider:
    return CodexProvider("term-d7", "sess", "win")


def _metadata() -> dict[str, Any]:
    return {"tmux_session": "sess", "tmux_window": "win", "provider_session_id": "uuid-d7"}


def _baseline() -> CodexSubmitBaseline:
    return CodexProvider._build_submission_baseline("• SEED_OK\n\n› Ask Codex to do anything\n")


@pytest.fixture()
def rollout(tmp_path: Path) -> Path:
    directory = tmp_path / "sessions" / "uuid-d7"
    directory.mkdir(parents=True)
    path = directory / "rollout-uuid-d7.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "uuid-d7"}}) + "\n",
        encoding="utf-8",
    )
    return path


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.get_history.return_value = STUCK_FRAME
    return backend


def _verify(provider: CodexProvider, backend: MagicMock, *, projected: bool) -> None:
    monitor = MagicMock()
    monitor.is_projected.return_value = projected
    monitor.get_status.return_value = TerminalStatus.PROCESSING
    with (
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor", monitor),
        patch("cli_agent_orchestrator.providers.codex.CODEX_ROLLOUT_POLL_TIMEOUT_SECONDS", 0.01),
        patch("cli_agent_orchestrator.providers.codex.CODEX_ROLLOUT_POLL_INTERVAL_SECONDS", 0.001),
    ):
        provider.verify_submission_after_send(
            _metadata(), backend, message=MESSAGE, baseline=_baseline()
        )


def test_a_projected_terminal_defers_instead_of_reading_the_pane(rollout: Path) -> None:
    """The whole of D7 in one assertion pair: no Enter, and a deferral.

    The pane here shows a stuck chip — exactly the frame the ladder was built to
    recover from — so a build that still entered the ladder would send a
    recovery Enter and this test would see it.
    """
    provider = _provider()
    provider._resolve_rollout_file = lambda _uuid: rollout  # type: ignore[assignment]
    backend = _backend()

    with pytest.raises(DeliveryDeferredError) as raised:
        _verify(provider, backend, projected=True)

    assert "deferring rather than recovering from the pane" in str(raised.value)
    backend.send_special_key.assert_not_called()


def test_an_unprojected_terminal_still_runs_the_ladder(rollout: Path) -> None:
    """The off arm.  With the cutover unset this is today's behaviour, and the
    ladder's recovery Enter is what "today's behaviour" means here."""
    provider = _provider()
    provider._resolve_rollout_file = lambda _uuid: rollout  # type: ignore[assignment]
    backend = _backend()

    def _submit_on_enter(_session: str, _window: str, key: str) -> None:
        if key == "Enter":
            with rollout.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": MESSAGE},
                        }
                    )
                    + "\n"
                )

    backend.send_special_key.side_effect = _submit_on_enter

    _verify(provider, backend, projected=False)

    assert backend.send_special_key.called


def test_a_monitor_that_cannot_answer_runs_the_ladder() -> None:
    """The fail-safe direction, and the reason it is ``is True`` rather than a
    truthiness test: a monitor replaced by a double answers with the double, and
    a double is truthy."""
    provider = _provider()
    monitor = MagicMock()  # is_projected returns a Mock, not a bool

    with patch("cli_agent_orchestrator.services.status_monitor.status_monitor", monitor):
        assert provider._projection_owns_submission() is False


def test_the_submission_event_confirms_without_touching_the_rollout_file(
    rollout: Path,
) -> None:
    """The cheaper primary signal.

    The same fact the file walk looks for, read from the log the tailer already
    wrote it to — one indexed query instead of a file walk plus a SQLite history
    scan, and it survives the substrate moving underneath (F643 and F643b are
    both "the transcript moved").
    """
    from cli_agent_orchestrator.core.events import EventKind

    provider = _provider()
    provider._resolve_rollout_file = lambda _uuid: rollout  # type: ignore[assignment]
    backend = _backend()
    baseline = _baseline()

    row = MagicMock()
    store = MagicMock()
    store.read.return_value = [row]
    runtime = MagicMock()
    runtime.event_store = store
    monitor = MagicMock()
    monitor.is_projected.return_value = True

    with (
        patch("cli_agent_orchestrator.bootstrap.current_runtime", return_value=runtime),
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor", monitor),
    ):
        provider.verify_submission_after_send(
            _metadata(), backend, message=MESSAGE, baseline=baseline
        )

    backend.send_special_key.assert_not_called()
    kwargs = store.read.call_args.kwargs
    assert kwargs["kinds"] == frozenset({EventKind.SUBMISSION_CONFIRMED})
    # Bounded by the dispatch baseline: an event from a PREVIOUS dispatch must
    # never confirm this one.
    assert kwargs["since"] == datetime.fromtimestamp(baseline.baseline_wall, UTC)


def test_with_worker_truth_off_the_event_check_is_inert(rollout: Path) -> None:
    """Phase 1's switch still gates everything: no runtime, no query, no change."""
    provider = _provider()
    provider._resolve_rollout_file = lambda _uuid: rollout  # type: ignore[assignment]
    backend = _backend()

    with patch("cli_agent_orchestrator.bootstrap.current_runtime", return_value=None):
        with pytest.raises(DeliveryDeferredError):
            _verify(provider, backend, projected=True)
