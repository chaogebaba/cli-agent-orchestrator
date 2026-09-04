"""F752 (#609) — a BUSY condition never outlives the turn it describes.

The reported row: fleet terminal ``1243fb68`` (cline_general, parked since its
08:21Z callback) rendered ``◌ idle [BUSY] 2h``, with the live API returning
``status=idle`` and ``condition=BUSY`` together. Two independent causes, and one
arm here for each:

1. **The latch.** The F611 fan-out writes the fleet ``condition`` field on every
   published status transition and only ever clears it when the classifier comes
   back empty. On the transition INTO idle the classifier still returned BUSY,
   so the label was re-affirmed instead of dropped, and it stuck for hours.
2. **The re-emission.** ``_classify_busy`` scanned the whole banner-row set. For
   every other provider the busy anchor is a spinner that erases itself, but
   cline's ``[thinking]``/``[run_commands]`` are printed LOG lines that stay on
   the pane forever — which is how a terminal parked at 08:21Z emitted a fresh
   ``tool_churn`` condition at 10:05:48Z off a static screen.

The panes below are SYNTHETIC (the live pane was not captured); they carry only
the anchors named in ``providers/condition.py``, which is all these arms assert
against. The TUI half of the fix has its own arms in
``test/tui/test_status_cell.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.condition import (
    BUSY_TAIL_ROWS,
    ConditionDelivery,
    ConditionKind,
    classify_condition,
    is_busy_class_label,
)

# A cline pane mid-turn: the tool-churn anchor is the last thing printed.
CLINE_WORKING_PANE = "\n".join(
    [
        "cline v3",
        "[thinking] planning the edit",
        "[run_commands] rg -n 'condition' src/",
    ]
)

# The same pane after the turn ended: the log lines are still on screen, but so
# is the completion. Nothing erases them — this is the shape that latched.
CLINE_PARKED_PANE = "\n".join(
    [
        "cline v3",
        "[thinking] planning the edit",
        "[run_commands] rg -n 'condition' src/",
        "[use_mcp_tool] cao-mcp-server send_message",
        "Task completed.",
        "> ",
    ]
)


class _ClineProvider:
    """Stands in for a live ClineProvider at the status-monitor seam."""

    def classify_condition(self, pane: str, **_kw: object) -> object:
        return classify_condition(pane, "cline_cli")


def _recording_delivery() -> Tuple[ConditionDelivery, Dict[str, List[Any]]]:
    rec: Dict[str, List[Any]] = {"fleet": [], "inbox": [], "cli": []}
    delivery = ConditionDelivery(
        fleet_sink=lambda tid, label: rec["fleet"].append((tid, label)),
        inbox_sink=lambda tid, cond: rec["inbox"].append((tid, cond.kind.value)),
        cli_sink=lambda tid, cond, label: rec["cli"].append((tid, label)),
    )
    return delivery, rec


def _monitor(tid: str) -> Any:
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    sm = StatusMonitor()
    sm._buffer_epochs[tid] = 1
    return sm


# ── Part 1a: the delivery seam refuses BUSY on a quiescent transition ──────────


def test_busy_is_not_written_on_a_transition_to_idle() -> None:
    """The transition INTO idle must clear the label, not re-affirm it."""
    tid = "1243fb68"
    sm = _monitor(tid)
    delivery, rec = _recording_delivery()
    sm._condition_delivery = delivery

    sm._classify_and_deliver_condition(
        tid, _ClineProvider(), CLINE_PARKED_PANE, status=TerminalStatus.IDLE
    )

    assert rec["fleet"] == [(tid, None)], "the transition must CLEAR the fleet label"
    assert rec["inbox"] == [], "a suppressed BUSY fires no inbox push"
    assert rec["cli"] == [], "a suppressed BUSY fires no CLI projection"


def test_busy_is_not_written_on_a_transition_to_completed() -> None:
    tid = "1243fb69"
    sm = _monitor(tid)
    delivery, rec = _recording_delivery()
    sm._condition_delivery = delivery

    sm._classify_and_deliver_condition(
        tid, _ClineProvider(), CLINE_PARKED_PANE, status=TerminalStatus.COMPLETED
    )
    assert rec["fleet"] == [(tid, None)]


def test_busy_is_delivered_normally_while_the_seat_is_working() -> None:
    """Only idle/completed contradict BUSY; the working case is untouched."""
    tid = "1243fb70"
    sm = _monitor(tid)
    delivery, rec = _recording_delivery()
    sm._condition_delivery = delivery

    sm._classify_and_deliver_condition(
        tid, _ClineProvider(), CLINE_WORKING_PANE, status=TerminalStatus.PROCESSING
    )
    assert rec["fleet"] == [(tid, "BUSY")]
    assert rec["cli"] == [(tid, "BUSY")]


def test_an_unknown_status_still_delivers_busy() -> None:
    """`unknown` is not a statement of rest — the pane may be mid-turn."""
    tid = "1243fb71"
    sm = _monitor(tid)
    delivery, rec = _recording_delivery()
    sm._condition_delivery = delivery

    sm._classify_and_deliver_condition(
        tid, _ClineProvider(), CLINE_WORKING_PANE, status=TerminalStatus.UNKNOWN
    )
    assert rec["fleet"] == [(tid, "BUSY")]


def test_a_real_condition_is_never_suppressed_by_an_idle_status() -> None:
    """A cap is TRUE of a resting seat: only the BUSY class is contradicted."""
    tid = "1243fb72"
    sm = _monitor(tid)
    delivery, rec = _recording_delivery()
    sm._condition_delivery = delivery
    # Not phrased "You have reached ..." on purpose: `banner_rows` treats a row
    # starting with "You" as quoted user text and drops it before any matcher.
    capped_pane = "cline v3\nClinePass limit reached\n> "

    sm._classify_and_deliver_condition(
        tid, _ClineProvider(), capped_pane, status=TerminalStatus.IDLE
    )
    assert rec["fleet"] == [(tid, "CAPPED")]
    assert rec["inbox"] == [(tid, "CAPPED")]


def test_the_latch_clears_when_the_seat_goes_idle() -> None:
    """The end-to-end latch: BUSY while working, gone the moment work stops."""
    tid = "1243fb73"
    sm = _monitor(tid)
    sm._condition_delivery = ConditionDelivery(
        fleet_sink=sm._condition_fleet_sink,
        inbox_sink=lambda _tid, _cond: None,
        cli_sink=lambda _tid, _cond, _label: None,
    )
    provider = _ClineProvider()

    sm._classify_and_deliver_condition(
        tid, provider, CLINE_WORKING_PANE, status=TerminalStatus.PROCESSING
    )
    assert sm.get_condition(tid) == "BUSY"

    sm._buffer_epochs[tid] = 2
    sm._classify_and_deliver_condition(tid, provider, CLINE_PARKED_PANE, status=TerminalStatus.IDLE)
    assert sm.get_condition(tid) is None, "the BUSY latch must not survive the turn"


def test_omitting_the_status_keeps_the_f611_behaviour() -> None:
    """The guard is opt-in per call site; F611's own arms pass no status."""
    tid = "1243fb74"
    sm = _monitor(tid)
    delivery, rec = _recording_delivery()
    sm._condition_delivery = delivery

    sm._classify_and_deliver_condition(tid, _ClineProvider(), CLINE_WORKING_PANE)
    assert rec["fleet"] == [(tid, "BUSY")]


# ── Part 1b: the egress read drops a stale label the fused status contradicts ──


def test_get_condition_drops_a_busy_label_for_a_quiescent_status() -> None:
    tid = "1243fb75"
    sm = _monitor(tid)
    sm._condition_fleet_sink(tid, "BUSY")

    assert sm.get_condition(tid, TerminalStatus.IDLE) is None
    assert sm.get_condition(tid, TerminalStatus.COMPLETED) is None
    assert sm.get_condition(tid, TerminalStatus.PROCESSING) == "BUSY"
    assert sm.get_condition(tid, TerminalStatus.UNKNOWN) == "BUSY"
    assert sm.get_condition(tid) == "BUSY", "no status argument = the F611 read"


def test_get_condition_keeps_every_other_label_on_an_idle_seat() -> None:
    tid = "1243fb76"
    sm = _monitor(tid)
    for label in ("CAPPED", "AUTH", "BLOCKED", "PROC_EXITED", "TRANSIENT_OVERLOAD"):
        sm._condition_fleet_sink(tid, label)
        assert sm.get_condition(tid, TerminalStatus.IDLE) == label


def test_busy_class_membership_is_exactly_busy() -> None:
    assert is_busy_class_label("BUSY") is True
    assert is_busy_class_label(None) is False
    for kind in ConditionKind:
        if kind is not ConditionKind.BUSY:
            assert is_busy_class_label(kind.value) is False


def test_fleet_row_drops_a_stale_busy_on_an_idle_seat(monkeypatch: Any) -> None:
    """`build_fleet` passes the fused status with the read, so the row the TUI
    and `cao fleet` receive never carries the contradiction."""
    import cli_agent_orchestrator.services.fleet_service as fs
    from cli_agent_orchestrator.services.status_monitor import status_monitor

    tid = "1243fb77"
    monkeypatch.setattr(
        fs,
        "list_terminals_by_session",
        lambda _s: [
            {
                "id": tid,
                "agent_profile": "secretary",
                "provider": "cline_cli",
                "tmux_window": "w0",
                "caller_id": None,
                "last_active": None,
                "lifecycle": "ephemeral",
            }
        ],
    )

    class _Backend:
        def get_session_windows(self, _s: str) -> List[Any]:
            # The row's window must be present or the projection stamps ERROR.
            return [{"name": "w0", "index": 0}]

    monkeypatch.setattr(fs, "get_backend", lambda: _Backend())

    class _Observation:
        status = TerminalStatus.IDLE
        fusion_changed = False
        fusion_reason = None

    monkeypatch.setattr(fs.status_monitor, "get_boundary_observation", lambda _tid: _Observation())
    monkeypatch.setattr(fs, "_compute_init_health", lambda _row, _now: "ok")
    status_monitor._condition_fleet_sink(tid, "BUSY")
    try:
        rows = {r["id"]: r for r in fs.build_fleet("sess-f752")["terminals"]}
        assert rows[tid]["status"] == "idle"
        assert rows[tid]["condition"] is None, "no `idle [BUSY]` row may reach the TUI"
    finally:
        status_monitor._condition_fleet_sink(tid, None)


# ── Part 1c: the cline busy anchor is only believable in the live tail ─────────


def test_cline_busy_matches_in_the_tail() -> None:
    cond = classify_condition(CLINE_WORKING_PANE, "cline_cli")
    assert cond is not None and cond.kind is ConditionKind.BUSY
    assert cond.subtype == "tool_churn"


def test_cline_busy_ignores_a_churn_line_scrolled_out_of_the_tail() -> None:
    """The 10:05:48Z re-emission: a static pane whose only churn is ancient.

    A whole-buffer scan matched the ``[run_commands]`` line no matter how far
    up it had scrolled, which is what made a two-hour-parked terminal look busy.
    """
    pane = "\n".join(
        ["[run_commands] rg -n 'condition' src/"]
        + [f"output line {i}" for i in range(BUSY_TAIL_ROWS + 10)]
        + ["Task completed.", "> "]
    )
    assert classify_condition(pane, "cline_cli") is None


def test_a_cap_far_above_the_tail_is_still_found() -> None:
    """The tail window is BUSY-only: a banner condition still scans the pane."""
    pane = "\n".join(
        ["ClinePass limit reached"] + [f"output line {i}" for i in range(BUSY_TAIL_ROWS + 10)]
    )
    cond = classify_condition(pane, "cline_cli")
    assert cond is not None and cond.kind is ConditionKind.CAPPED


# ── The production poll site fans the cleared condition out ────────────────────


def test_apply_detection_transition_to_idle_clears_the_busy_latch(monkeypatch: Any) -> None:
    """The real transition path: a working seat carrying BUSY goes idle and the
    fleet field is cleared by the same seam that set it."""
    import cli_agent_orchestrator.clients.database as db_mod
    import cli_agent_orchestrator.services.status_monitor as sm_mod

    tid = "1243fb78"
    sm = _monitor(tid)
    delivery, rec = _recording_delivery()
    sm._condition_delivery = delivery
    sm._buffers[tid] = CLINE_PARKED_PANE
    sm._last_status[tid] = TerminalStatus.PROCESSING

    monkeypatch.setattr(sm_mod.provider_manager, "get_provider", lambda _tid: _ClineProvider())
    monkeypatch.setattr(db_mod, "get_terminal_metadata", lambda _tid: {"id": _tid})
    monkeypatch.setattr(sm, "_publish_observation", lambda *a, **k: None)
    monkeypatch.setattr(db_mod, "reconcile_children_on_publish", lambda *a, **k: None)

    sm._apply_detection(tid, TerminalStatus.IDLE)

    assert rec["fleet"] == [(tid, None)], "the idle transition must clear the label"
    assert rec["inbox"] == [], "a parked seat must not push a BUSY event"
