"""D8 — the condition label gets a lifetime, and stops reading the server's text.

F611 gave the label one driver: the genuine-transition branch of pane detection.
So a label is set at a transition and never revisited, and a terminal that has
gone quiet produces no transition by definition — which is exactly when a stale
label sits on the fleet row longest. D8 adds the projector's sweep as a second
driver and bounds the lifetime by ``PANE_HEARTBEAT_S``.

Both legs are gated on the cutover, and the gate is the point rather than
caution: for an unsourced terminal the label keeps F611's driver and F752's
read-side suppression exactly as they are. #609 is closed, and this phase must
not reopen it by making an unsourced fleet's condition behaviour depend on the
cutover being off.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import StatusMonitor

TERMINAL = "t-cond"
EXIT_LINE = "   ⎿ [Command exited with code 1]"


class _View:
    def __init__(self, projected: set[str]) -> None:
        self.projected = projected

    def is_projected(self, terminal_id: str) -> bool:
        return terminal_id in self.projected


def _monitor(projected: set[str] | None = None) -> StatusMonitor:
    monitor = StatusMonitor()
    if projected is not None:
        monitor.enable_projection(_View(projected))
    return monitor


def _classified(monitor: StatusMonitor) -> list[object]:
    """Run the re-drive and return what reached the delivery seam."""
    delivered: list[object] = []
    provider = MagicMock()
    provider.classify_condition.side_effect = lambda buffer: buffer
    delivery = MagicMock()
    delivery.deliver.side_effect = lambda terminal_id, cond, **kw: delivered.append(cond)
    with (
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=provider,
        ),
        patch.object(monitor, "_get_condition_delivery", return_value=delivery),
    ):
        monitor.reclassify_condition(TERMINAL)
    return delivered


# ------------------------------------------------------ the sweep leg (D8)


def test_a_projected_terminal_is_reclassified_off_a_transition() -> None:
    """AC-2b case 3's mechanism.  The label's lifetime stops depending on the
    worker making its next move."""
    monitor = _monitor({TERMINAL})
    with monitor._lock:
        monitor._buffers[TERMINAL] = "some pane text"
        monitor._last_status[TERMINAL] = TerminalStatus.PROCESSING

    assert _classified(monitor) == ["some pane text"]


def test_an_unsourced_terminal_is_left_alone() -> None:
    """I7 at the condition seam.  F752's read-side suppression is the fallback
    for these terminals and is deliberately untouched — re-driving here would
    make their behaviour depend on this phase, which is #609 reopened."""
    monitor = _monitor(set())
    with monitor._lock:
        monitor._buffers[TERMINAL] = "some pane text"

    assert _classified(monitor) == []


def test_with_the_cutover_off_nothing_is_reclassified() -> None:
    monitor = StatusMonitor()  # no view at all
    with monitor._lock:
        monitor._buffers[TERMINAL] = "some pane text"

    assert _classified(monitor) == []


def test_the_re_drive_passes_the_published_status_without_fusing() -> None:
    """F752's own parameter exists so this seam can hand over the status the
    caller already holds.  Fusing from a sweep thread would re-enter the read
    rules for no gain."""
    monitor = _monitor({TERMINAL})
    with monitor._lock:
        monitor._buffers[TERMINAL] = "text"
        monitor._last_status[TERMINAL] = TerminalStatus.IDLE
    seen: list[object] = []

    with (
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=MagicMock(),
        ),
        patch.object(
            monitor,
            "_classify_and_deliver_condition",
            lambda terminal_id, provider, buffer, status=None: seen.append(status),
        ),
        patch.object(
            monitor, "fuse_status", side_effect=AssertionError("the re-drive must not fuse")
        ),
    ):
        monitor.reclassify_condition(TERMINAL)

    assert seen == [TerminalStatus.IDLE]


def test_a_broken_provider_never_breaks_the_sweep() -> None:
    """The re-drive rides the sweep, and the sweep is ``degraded(no_signal)``'s
    only producer."""
    monitor = _monitor({TERMINAL})

    with patch(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        side_effect=RuntimeError("provider gone"),
    ):
        monitor.reclassify_condition(TERMINAL)  # must not raise


# --------------------------------------------- the delivered-text anchor (#545)


def test_a_condition_anchor_the_server_delivered_is_not_evidence() -> None:
    """AC-2b case 4.  #545 verbatim: the server pings itself about its own text.

    The classifier reads the rolling output buffer, and the pane echoes whatever
    the server pastes — so a message whose body quotes an exit line comes back as
    the worker's evidence.
    """
    monitor = _monitor({TERMINAL})
    monitor.note_delivered_text(TERMINAL, f"please look at this:\n{EXIT_LINE}\nthanks")
    with monitor._lock:
        monitor._buffers[TERMINAL] = f"[run_commands] rg foo\n{EXIT_LINE}"

    assert _classified(monitor) == ["[run_commands] rg foo"]


def test_the_worker_s_own_text_is_still_evidence() -> None:
    """The filter drops what the SERVER wrote, not what the pane produced."""
    monitor = _monitor({TERMINAL})
    monitor.note_delivered_text(TERMINAL, "a message with no anchor in it")
    with monitor._lock:
        monitor._buffers[TERMINAL] = f"[run_commands] rg foo\n{EXIT_LINE}"

    assert _classified(monitor) == [f"[run_commands] rg foo\n{EXIT_LINE}"]


def test_an_unprojected_terminal_reads_its_buffer_whole() -> None:
    """The off arm of case 4: with the cutover off, the buffer is what it was."""
    monitor = _monitor(set())
    monitor.note_delivered_text(TERMINAL, EXIT_LINE)
    with monitor._lock:
        monitor._buffers[TERMINAL] = EXIT_LINE
    delivered: list[object] = []
    provider = MagicMock()
    provider.classify_condition.side_effect = lambda buffer: buffer
    delivery = MagicMock()
    delivery.deliver.side_effect = lambda terminal_id, cond, **kw: delivered.append(cond)

    with patch.object(monitor, "_get_condition_delivery", return_value=delivery):
        monitor._classify_and_deliver_condition(TERMINAL, provider, EXIT_LINE)

    assert delivered == [EXIT_LINE]


def test_each_delivery_replaces_the_last() -> None:
    """One message per terminal.  Accumulating would be a leak that also
    suppressed more and more real evidence as the session went on."""
    monitor = _monitor({TERMINAL})
    monitor.note_delivered_text(TERMINAL, "first delivered line here")
    monitor.note_delivered_text(TERMINAL, "second delivered line here")
    with monitor._lock:
        monitor._buffers[TERMINAL] = "first delivered line here\nsecond delivered line here"

    assert _classified(monitor) == ["first delivered line here"]


def test_short_lines_are_not_remembered() -> None:
    """A condition anchor is a distinctive string; excluding a bare ``ok`` stops
    the filter blinding the classifier to genuine rows that share it."""
    monitor = _monitor({TERMINAL})
    monitor.note_delivered_text(TERMINAL, "ok\nyes")
    with monitor._lock:
        monitor._buffers[TERMINAL] = "ok\nyes"

    assert _classified(monitor) == ["ok\nyes"]


@pytest.mark.parametrize("message", ["", "   \n\n"])
def test_an_empty_delivery_clears_rather_than_holds(message: str) -> None:
    monitor = _monitor({TERMINAL})
    monitor.note_delivered_text(TERMINAL, "an earlier delivered line")
    monitor.note_delivered_text(TERMINAL, message)
    with monitor._lock:
        monitor._buffers[TERMINAL] = "an earlier delivered line"

    assert _classified(monitor) == ["an earlier delivered line"]
