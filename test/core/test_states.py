"""AC1 — the transition classifier, all 49 cells (WP-ARCH phase 1, F725 #581).

The table-driven test the blueprint asks for is deliberately written as an
INDEPENDENT transcription of the audit §3.1 grid rather than as a loop over
``TRANSITIONS``.  A test that derives its expectations from the thing under test
passes for any table, including a loosened one, and "transition table loosened"
is a phase-1 mutant.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.core.states import (
    DEGRADED_REASON_RANK,
    STRICT_ENV_VAR,
    TRANSITIONS,
    DegradedReason,
    TransitionClass,
    WorkerState,
    reason_rises,
    validate,
)

A = TransitionClass.ALLOWED
N = TransitionClass.NO_OP
X = TransitionClass.ANOMALOUS

# Audit §3.1, transcribed by hand.  Column order:
#   starting, idle, busy, awaiting_input, capped, degraded, exited
_EXPECTED_GRID: dict[str, tuple[TransitionClass, ...]] = {
    "starting": (N, A, A, A, A, A, A),
    "idle": (X, N, A, A, A, A, A),
    "busy": (X, A, N, A, A, A, A),
    "awaiting_input": (X, A, A, N, A, A, A),
    "capped": (X, A, X, X, N, A, A),
    "degraded": (X, A, A, A, A, N, A),
    "exited": (A, X, X, X, X, X, N),
}
_COLUMNS = (
    "starting",
    "idle",
    "busy",
    "awaiting_input",
    "capped",
    "degraded",
    "exited",
)

_CELLS = [
    (WorkerState(row), WorkerState(column), _EXPECTED_GRID[row][index])
    for row in _EXPECTED_GRID
    for index, column in enumerate(_COLUMNS)
]


def test_grid_covers_every_state() -> None:
    """The hand-transcribed grid is 7x7 over the real enum, not a subset."""
    assert set(_EXPECTED_GRID) == {state.value for state in WorkerState}
    assert set(_COLUMNS) == {state.value for state in WorkerState}
    assert len(_CELLS) == 49


@pytest.mark.parametrize(("from_state", "to_state", "expected"), _CELLS)
def test_validate_classifies_every_cell(
    from_state: WorkerState, to_state: WorkerState, expected: TransitionClass
) -> None:
    """All 49 cells classify exactly as the audit table says."""
    assert validate(from_state, to_state) is expected


def test_transitions_table_is_total() -> None:
    """``TRANSITIONS`` has an explicit entry per cell — no implicit default."""
    assert len(TRANSITIONS) == 49
    for from_state in WorkerState:
        for to_state in WorkerState:
            assert (from_state, to_state) in TRANSITIONS


def test_classification_census() -> None:
    """Counts pin the table's shape: 7 no-ops, 12 anomalous cells, 30 allowed.

    A loosened table almost always shows up here first — turning any anomalous
    cell into an allowed one moves both counts.
    """
    census = {
        classification: sum(1 for value in TRANSITIONS.values() if value is classification)
        for classification in TransitionClass
    }
    assert census == {
        TransitionClass.NO_OP: 7,
        TransitionClass.ANOMALOUS: 12,
        TransitionClass.ALLOWED: 30,
    }


def test_diagonal_is_always_a_noop() -> None:
    """Same-state re-entry is legal everywhere and never a transition."""
    for state in WorkerState:
        assert validate(state, state) is TransitionClass.NO_OP


def test_only_respawn_re_enters_starting() -> None:
    """``exited -> starting`` is the single legal arrival into ``starting``."""
    into_starting = {
        from_state: TRANSITIONS[(from_state, WorkerState.STARTING)] for from_state in WorkerState
    }
    assert into_starting[WorkerState.EXITED] is TransitionClass.ALLOWED
    assert into_starting[WorkerState.STARTING] is TransitionClass.NO_OP
    for from_state, classification in into_starting.items():
        if from_state not in (WorkerState.EXITED, WorkerState.STARTING):
            assert classification is TransitionClass.ANOMALOUS


def test_exited_is_terminal_except_for_respawn() -> None:
    """Nothing leaves ``exited`` but a respawn; every other exit edge is anomalous."""
    for to_state in WorkerState:
        expected = {
            WorkerState.STARTING: TransitionClass.ALLOWED,
            WorkerState.EXITED: TransitionClass.NO_OP,
        }.get(to_state, TransitionClass.ANOMALOUS)
        assert validate(WorkerState.EXITED, to_state) is expected


def test_capped_to_busy_is_anomalous() -> None:
    """The AC6 worked example: a rollout reporting ``capped -> busy``.

    It is applied by the projector AND flagged; the classifier's job is only the
    flag.
    """
    assert validate(WorkerState.CAPPED, WorkerState.BUSY) is TransitionClass.ANOMALOUS


def test_validate_never_raises_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the strict flag, every cell returns rather than raises.

    This is r9's central rule: the projector observes a foreign process, it does
    not authorise it.
    """
    monkeypatch.delenv(STRICT_ENV_VAR, raising=False)
    for from_state in WorkerState:
        for to_state in WorkerState:
            assert isinstance(validate(from_state, to_state), TransitionClass)


def test_strict_flag_raises_only_on_anomalous(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CAO_WORKER_TRUTH_STRICT=1`` turns anomalous cells into errors, for tests only."""
    monkeypatch.setenv(STRICT_ENV_VAR, "1")
    with pytest.raises(ValueError, match="anomalous worker transition"):
        validate(WorkerState.CAPPED, WorkerState.BUSY)
    assert validate(WorkerState.IDLE, WorkerState.BUSY) is TransitionClass.ALLOWED
    assert validate(WorkerState.IDLE, WorkerState.IDLE) is TransitionClass.NO_OP


def test_strict_flag_is_read_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting and clearing the flag takes effect immediately — no import-time cache."""
    monkeypatch.setenv(STRICT_ENV_VAR, "1")
    with pytest.raises(ValueError):
        validate(WorkerState.EXITED, WorkerState.IDLE)
    monkeypatch.setenv(STRICT_ENV_VAR, "0")
    assert validate(WorkerState.EXITED, WorkerState.IDLE) is TransitionClass.ANOMALOUS


def test_degraded_reason_enum_is_closed_and_ranked() -> None:
    """Every reason has a rank, and the ranks are the blueprint's strict order."""
    assert set(DEGRADED_REASON_RANK) == set(DegradedReason)
    assert len(set(DEGRADED_REASON_RANK.values())) == len(DegradedReason)
    ordered = sorted(DegradedReason, key=lambda reason: DEGRADED_REASON_RANK[reason], reverse=True)
    assert ordered == [
        DegradedReason.PRODUCER_ERROR,
        DegradedReason.ROLLOUT_MISSING,
        DegradedReason.CONFLICTING_SOURCES,
        DegradedReason.PANE_UNREADABLE,
        DegradedReason.RENDER_UNCERTAIN,
        DegradedReason.NO_SIGNAL,
    ]


def test_reason_rises_only_upward() -> None:
    """A degraded->degraded arrival changes the reason only when it outranks."""
    assert reason_rises(None, DegradedReason.NO_SIGNAL) is True
    assert reason_rises(DegradedReason.NO_SIGNAL, DegradedReason.PRODUCER_ERROR) is True
    assert reason_rises(DegradedReason.PRODUCER_ERROR, DegradedReason.NO_SIGNAL) is False
    assert reason_rises(DegradedReason.PANE_UNREADABLE, DegradedReason.PANE_UNREADABLE) is False
