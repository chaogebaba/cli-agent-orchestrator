"""D1c/D1f on the herdr backend (WP-ARCH 2b).

The defect these tests pin: ``status.pane_classified`` — and with it D1c's
``usage.capped`` and D1f's ``prompt.awaiting``/``prompt.answered`` — was never
produced on the herdr path. Not refused, not renamed, not gated on
certification: never invoked.

``record_pane_classification`` had exactly one call site, the ``finally`` block
of ``StatusMonitor._apply_detection``, and every driver of ``_apply_detection``
hangs off ``_process_chunk``. An event-inbox backend starts no FIFO reader, so
nothing feeds that pipeline and the whole producer family is silent. The one
pane-driven path that does reach ``_apply_detection`` —
``resync_from_pane_tail`` — cannot cover it: its ``dropped`` trigger needs a
stream herdr does not have, and its ``periodic`` trigger needs ``_last_status``
in {PROCESSING, ERROR} while ``_last_status`` is written ONLY by
``_apply_detection`` and the projection publisher, so on an unsourced herdr
terminal it is UNKNOWN forever and the backstop deadlocks against itself.

Measured shape this reproduces (round 3, box 010, both arms): 0
``status.pane_classified``, 85 ``status.legacy_published`` all carrying
``origin=native`` — the native path, which bypasses ``_apply_detection``
entirely — and ``status_gen: 0`` on every terminal in the read path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.adapters.truth import pane_classification, wiring
from cli_agent_orchestrator.core.events import EventKind, Producer
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import StatusMonitor
from cli_agent_orchestrator.utils import herdr_runtime_gate

TERMINAL = "t-herdr"
TAIL = "esc to interrupt\n> \n"


# ------------------------------------------------------------------ doubles


class _Clock:
    def __init__(self) -> None:
        self._now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        value = self._now
        self._now += timedelta(seconds=1)
        return value


class _Store:
    """Enough ``EventStore`` for the producer: append, mint, remember."""

    def __init__(self) -> None:
        self.rows: list[object] = []

    def append(self, draft):
        from cli_agent_orchestrator.core.events import WorkerEvent

        event = WorkerEvent(
            **draft.model_dump(),
            event_id=f"e{len(self.rows) + 1:04d}",
            seq=len(self.rows) + 1,
            ingested_at=datetime.now(timezone.utc),
        )
        self.rows.append(event)
        return event

    def kinds(self) -> list[EventKind]:
        return [row.kind for row in self.rows]

    def of_kind(self, kind: EventKind) -> list[object]:
        return [row for row in self.rows if row.kind == kind]


class _Backend:
    def __init__(self, event_inbox: bool) -> None:
        self._event_inbox = event_inbox

    def supports_event_inbox(self) -> bool:
        return self._event_inbox


class _Provider:
    """A provider the snapshot router will admit (``supports_screen_detection``)."""

    supports_stale_capture_selfheal = True
    supports_screen_detection = True
    supports_direct_status_probe = False

    def __init__(self, status: TerminalStatus = TerminalStatus.PROCESSING) -> None:
        self.status = status
        self.seen: list[list[str]] = []

    def get_status_from_screen(self, lines):
        self.seen.append(lines)
        return self.status


class _OpaqueProvider:
    """Raw-stream-tuned: ``_snapshot_detector_mode`` must refuse it."""

    supports_stale_capture_selfheal = True
    supports_screen_detection = False
    supports_direct_status_probe = False

    def get_status_from_screen(self, lines):  # pragma: no cover - must not be called
        raise AssertionError("a rendered frame reached a raw-stream-tuned detector")


# ----------------------------------------------------------------- fixtures


@pytest.fixture
def store():
    recorder = _Store()
    wiring.install_producers(wiring.ProducerRuntime(store=recorder, clock=_Clock()))
    pane_classification.reset_edges()
    try:
        yield recorder
    finally:
        wiring.reset_producers()
        pane_classification.reset_edges()


@pytest.fixture(autouse=True)
def _clean_gate():
    herdr_runtime_gate.reset_gate()
    yield
    herdr_runtime_gate.reset_gate()


def _run(
    monitor: StatusMonitor,
    provider: object,
    *,
    event_inbox: bool = True,
    tail: str = TAIL,
) -> bool:
    with (
        patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=_Backend(event_inbox),
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=provider,
        ),
    ):
        return monitor.classify_pane_sample(TERMINAL, tail)


# ------------------------------------------- the gap itself: herdr produces


def test_an_unsourced_herdr_lane_is_pane_classified(store):
    """The round-4 blocker. An unsourced herdr lane must reach the cohort.

    ``unsourced-identical`` builds its cohort from ``status.pane_classified``
    and an EMPTY cohort is a SKIP, never a pass — so with this kind missing the
    live verdict is NO however many unsourced lanes the round spawns.
    """
    assert _run(StatusMonitor(), _Provider()) is True

    rows = store.of_kind(EventKind.STATUS_PANE_CLASSIFIED)
    assert len(rows) == 1
    assert rows[0].terminal_id == TERMINAL
    assert rows[0].producer is Producer.PANE
    assert rows[0].payload["latched_status"] == "processing"


def test_the_row_records_the_classifier_verdict_as_both_latched_and_raw(store):
    """No latch stands between them on this path, and saying so is the honest form.

    Leaving ``raw_classification`` null would let a reader infer a latch that
    never ran.
    """
    _run(StatusMonitor(), _Provider(TerminalStatus.IDLE))
    payload = store.of_kind(EventKind.STATUS_PANE_CLASSIFIED)[0].payload
    assert payload["latched_status"] == "idle"
    assert payload["raw_classification"] == "idle"
    assert payload["frame_source"] == "fresh_capture"


def test_the_pass_is_edge_triggered_not_once_per_sampler_tick(store):
    """The sampler runs every few seconds for the life of the worker.

    A row per tick would dominate the log — the write-rate property §9 promises
    to measure — so the pass reuses the producer's own ``(status, origin)`` edge.
    """
    monitor, provider = StatusMonitor(), _Provider()
    for _ in range(40):
        _run(monitor, provider)
    assert len(store.of_kind(EventKind.STATUS_PANE_CLASSIFIED)) == 1

    provider.status = TerminalStatus.IDLE
    _run(monitor, provider)
    assert len(store.of_kind(EventKind.STATUS_PANE_CLASSIFIED)) == 2


# --------------------------------------------- inert where the chunk path runs


def test_a_pipe_pane_backend_is_left_to_apply_detection(store):
    """tmux classifies on every chunk already; a second producer would race it.

    The tmux round recorded 252 of these rows through ``_apply_detection``. This
    pass must add nothing there.
    """
    assert _run(StatusMonitor(), _Provider(), event_inbox=False) is False
    assert store.rows == []


# ----------------------------------------------------- fails closed, always


@pytest.mark.parametrize(
    "provider,tail",
    [
        (None, TAIL),
        (_OpaqueProvider(), TAIL),
        (_Provider(), ""),
    ],
    ids=["no-provider", "raw-stream-tuned-detector", "empty-sample"],
)
def test_no_verdict_means_no_row(store, provider, tail):
    assert _run(StatusMonitor(), provider, tail=tail) is False
    assert store.rows == []


def test_a_detector_that_raises_never_reaches_the_sampler(store):
    provider = MagicMock(spec=["supports_screen_detection", "get_status_from_screen"])
    provider.supports_screen_detection = True
    provider.get_status_from_screen.side_effect = RuntimeError("boom")
    assert _run(StatusMonitor(), provider) is False
    assert store.rows == []


# ------------------------------------- D1f/D1c ride the same pass (amendment)


def test_the_dialog_edge_comes_through_on_herdr(store):
    """§5: the herdr source maps ``blocked`` to NOTHING and emits no dialog kind.

    So for a herdr lane the pane is the only producer of ``prompt.awaiting``,
    which is what the §6 amendment means by "dialog cards still apply from the
    pane". codex is the case that needs it most: no dialog hook at all.
    """
    monitor, provider = StatusMonitor(), _Provider(TerminalStatus.WAITING_USER_ANSWER)
    _run(monitor, provider)
    assert EventKind.PROMPT_AWAITING in store.kinds()

    provider.status = TerminalStatus.IDLE
    _run(monitor, provider)
    assert EventKind.PROMPT_ANSWERED in store.kinds()


def test_the_vendor_cap_comes_through_on_herdr(store):
    """D1c's ``usage.capped``: the one thing a rollout can never report."""
    from cli_agent_orchestrator.adapters.truth.legacy_egress import CAPPED_CONDITION_LABEL

    monitor = StatusMonitor()
    with patch.object(monitor, "get_condition", return_value=CAPPED_CONDITION_LABEL, create=True):
        _run(monitor, _Provider())
    assert EventKind.USAGE_CAPPED in store.kinds()


def test_a_certified_lane_is_classified_too(store):
    """NOT gated on ``herdr_lifecycle_authoritative``.

    None of the three kinds this pass drives asserts a lifecycle state —
    ``status.pane_classified`` is deliberately absent from
    ``mapping.STATE_ASSERTING_KINDS`` — so the §6 amendment does not reach them,
    and gating here would make its own "vendor conditions and dialog cards still
    apply from the pane" false.
    """
    herdr_runtime_gate.bind_terminal(TERMINAL, True)
    with patch.dict("os.environ", {herdr_runtime_gate.HERDR_RUNTIME_ENV_VAR: "1"}):
        assert herdr_runtime_gate.herdr_lifecycle_authoritative(TERMINAL) is True
        assert _run(StatusMonitor(), _Provider()) is True
    assert len(store.of_kind(EventKind.STATUS_PANE_CLASSIFIED)) == 1


# --------------------------- the amendment's other half: no pane LIFECYCLE


def _observation(unchanged_count: int = 0):
    from cli_agent_orchestrator.services.pane_liveness import PaneObservation

    return PaneObservation(
        unchanged_count=unchanged_count,
        unchanged_for_s=1.0,
        pane_hold_expired=False,
        fingerprint="fp",
        fp_changed=True,
        filtered_tail=TAIL,
        busy_marker=True,
        children_count=0,
    )


def _fuse(certified: bool):
    monitor = StatusMonitor()
    with (
        patch(
            "cli_agent_orchestrator.services.pane_liveness.pane_liveness.peek",
            return_value=_observation(),
        ),
        patch(
            "cli_agent_orchestrator.services.status_monitor._herdr_lifecycle_authoritative",
            return_value=certified,
        ),
        patch(
            "cli_agent_orchestrator.services.question_state.question_state.is_open",
            return_value=False,
        ),
    ):
        return monitor.fuse_status(TERMINAL, TerminalStatus.IDLE)


def test_rule_3a_moves_an_uncertified_lane():
    """The pane IS a first-class fallback for an unsourced terminal (I7)."""
    assert _fuse(certified=False) == (TerminalStatus.PROCESSING, "pane_delta")


def test_rule_3a_is_inert_for_a_certified_lane():
    """§6: a certified terminal takes lifecycle from the herdr source.

    Rule 3a converting a published IDLE into PROCESSING on pane churn alone is
    exactly the pane lifecycle move the amendment disables. The gate lives here
    rather than in the sampler loop so the classification pass above can still
    run for the same terminal.
    """
    assert _fuse(certified=True) == (TerminalStatus.IDLE, None)
