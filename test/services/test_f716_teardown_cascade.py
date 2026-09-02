"""F716 (#571) r2 — every tmux kill in a delete happens under a teardown mark.

Round 1 gated the fleet projection on the F218 teardown intent, but left two
paths where a window is killed with no mark covering the row, so the fleet
still projects ERROR mid-teardown:

blocker 1 (``terminal_service.delete_terminal``) — an ``open_intent`` failure
was logged and the delete proceeded, killing the window with nothing recorded.

blocker 2 (``terminal_service._delete_terminal_inner``) — the cascade kills
every node in the reap plan while only the ROOT is bracketed, so cascaded
children project ERROR for the duration.

Both are closed by marking BEFORE the kill: an in-process mark for the root
that cannot fail (fallback for blocker 1) and a mark + durable intent per
cascaded child (blocker 2). These tests sample ``build_fleet`` from inside the
delete, between one node's window kill and the next, which is exactly the
window in which the operator saw ERROR.

The safety property of AC-F716-2 is asserted in the same samples: a row on the
same session whose window is gone but which is NOT part of the cascade must
still project ERROR. That is also what rules out "just open a session-scope
intent" as the fix.

Cascade harness mirrors ``test/services/test_f631_reap_resume_key.py``.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.backends import registry as backend_registry
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base, F218TeardownIntentModel
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import fleet_service, teardown_intent_service, terminal_service
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation

SESSION = "cao-f716r2"


def _window(terminal_id: str) -> str:
    return f"w-{terminal_id}"


class _Backend:
    """Fake tmux inventory. ``kill`` is what the delete path's stub calls."""

    def __init__(self, terminal_ids):
        self.windows = {_window(tid) for tid in terminal_ids}

    def get_session_windows(self, _session):
        return [{"name": w, "index": str(i)} for i, w in enumerate(sorted(self.windows))]

    def get_history(self, *_a, **_k):
        return ""

    def kill(self, terminal_id):
        self.windows.discard(_window(terminal_id))


@pytest.fixture
def env(monkeypatch):
    """In-memory DB + fake backend + a status monitor that publishes COMPLETED.

    Rows: root, two cascaded children, and ``strayaaa`` — a terminal on the
    same session that is NOT in the cascade, used as the safety control.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    database.clear_terminal_metadata_cache()

    # In-process marks are module state; never leak one into another test.
    monkeypatch.setattr(teardown_intent_service, "_MEMORY_MARKS", {})

    monkeypatch.setattr(
        fleet_service.status_monitor,
        "get_boundary_observation",
        lambda _tid: BoundaryObservation(
            observation_epoch="e",
            status=TerminalStatus.COMPLETED,
            status_gen=None,
            input_gen=0,
            seq=0,
            last_non_ready_seq=None,
            last_ready_seq=None,
        ),
    )
    backend = _Backend([])
    monkeypatch.setattr(backend_registry, "_backend", backend)
    return backend


def _create(terminal_id, backend, caller_id=None):
    database.create_terminal(
        terminal_id,
        SESSION,
        _window(terminal_id),
        "cline_cli",
        agent_profile="cline_dev",
        caller_id=caller_id,
    )
    backend.windows.add(_window(terminal_id))


def _arm_cascade(monkeypatch, under_lease):
    """Neutralise leases/guards/quiesce; route the kill through ``under_lease``.

    Everything the projection reads (DB rows, backend inventory, teardown
    marks) is left REAL — those are what the assertions are about.
    """
    from cli_agent_orchestrator.services.terminal_guard_service import DeletionClassification

    monkeypatch.setattr(terminal_service, "quiesce_deferred_terminal_sync", lambda tid, **kw: None)
    monkeypatch.setattr(terminal_service, "has_deferred_init", lambda tid: False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_guard_service.classify_deletion",
        lambda tid, force=False: DeletionClassification(True),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
        lambda tid, force=False: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_lifecycle_lease."
        "acquire_session_lifecycle_exclusive_blocking",
        lambda _s, timeout_s=5.0: "lease",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_lease",
        lambda _l: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
        lambda tid: MagicMock(terminal_id=tid),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease", lambda _t: None
    )
    monkeypatch.setattr(
        terminal_service,
        "status_monitor",
        MagicMock(
            get_boundary_observation=MagicMock(
                return_value=MagicMock(status=MagicMock(value="idle"))
            )
        ),
    )
    monkeypatch.setattr(terminal_service, "_delete_terminal_under_lease", under_lease)


def _sampling_under_lease(backend, samples):
    """Kill the node's window, then sample the fleet exactly as an operator would.

    The DB row is deliberately NOT purged: the sample is taken in the state the
    issue reports — row still live, window already gone.
    """

    def under_lease(node_id, token, **_kw):
        backend.kill(node_id)
        samples.append(
            {
                row["id"]: (row["status"], row["teardown"])
                for row in fleet_service.build_fleet(SESSION)["terminals"]
            }
        )
        return {"terminal_deleted": True, "resume_key": None}

    return under_lease


def _statuses_of(samples, terminal_id):
    return [sample[terminal_id][0] for sample in samples if terminal_id in sample]


# ── blocker 1: a failed intent open must not leave the kill unmarked ────────


def test_intent_open_failure_still_suppresses_the_teardown_error(env, monkeypatch):
    """The durable intent cannot be written; the delete proceeds on the mark.

    Aborting instead would be the other legal answer, but a delete is itself a
    recovery operation — refusing to reap a terminal because a bookkeeping
    table is unwritable turns a cosmetic projection defect into an
    availability defect. So the contract asserted here is: the delete still
    runs, and it still does not project ERROR.
    """
    _create("rootaaaa", env)
    _create("strayaaa", env)
    env.kill("strayaaa")  # gone with no delete in flight — the safety control

    def _boom(**_kw):
        raise RuntimeError("teardown intent table unavailable")

    monkeypatch.setattr(teardown_intent_service, "open_intent", _boom)

    samples = []
    _arm_cascade(monkeypatch, _sampling_under_lease(env, samples))

    result = terminal_service.delete_terminal("rootaaaa")

    assert [row["id"] for row in result["reaped"]] == ["rootaaaa"]
    assert len(samples) == 1
    # The root's window is gone at sample time, and no durable intent exists.
    assert _statuses_of(samples, "rootaaaa") == [TerminalStatus.COMPLETED.value]
    assert samples[0]["rootaaaa"][1] is True
    with database.SessionLocal() as db:
        assert db.query(F218TeardownIntentModel).count() == 0
    # Safety: the uninvolved row with a vanished window is still ERROR.
    assert samples[0]["strayaaa"] == (TerminalStatus.ERROR.value, False)
    # The mark is released once the delete returns.
    assert teardown_intent_service.active_teardown_scope_keys() == set()


# ── blocker 2: cascaded children are marked before the first kill ───────────


def test_cascaded_children_never_project_error_during_the_cascade(env, monkeypatch):
    """Root + two children: every sample taken between kills is ERROR-free."""
    _create("rootbbbb", env)
    _create("childaaa", env, caller_id="rootbbbb")
    _create("childbbb", env, caller_id="rootbbbb")
    _create("straybbb", env)
    env.kill("straybbb")  # the safety control again

    samples = []
    _arm_cascade(monkeypatch, _sampling_under_lease(env, samples))

    result = terminal_service.delete_terminal("rootbbbb")

    # Children before parent, all three reaped.
    assert [row["id"] for row in result["reaped"]] == ["childaaa", "childbbb", "rootbbbb"]
    assert len(samples) == 3

    for terminal_id in ("childaaa", "childbbb", "rootbbbb"):
        statuses = _statuses_of(samples, terminal_id)
        assert (
            TerminalStatus.ERROR.value not in statuses
        ), f"{terminal_id} projected ERROR mid-cascade: {statuses}"
    # The first child is killed first, so its window is already gone in the
    # samples that follow — this is the state r1 projected as ERROR.
    assert samples[1]["childaaa"] == (TerminalStatus.COMPLETED.value, True)
    assert samples[2]["childaaa"] == (TerminalStatus.COMPLETED.value, True)
    assert samples[2]["childbbb"] == (TerminalStatus.COMPLETED.value, True)

    # Safety in every sample: a window-gone row outside the cascade is ERROR,
    # and carries no teardown flag. A session-scope intent would fail this.
    for sample in samples:
        assert sample["straybbb"] == (TerminalStatus.ERROR.value, False)

    with database.SessionLocal() as db:
        assert db.query(F218TeardownIntentModel).count() == 0
    assert teardown_intent_service.active_teardown_scope_keys() == set()


def test_children_are_marked_before_the_first_kill_not_as_each_is_reached(env, monkeypatch):
    """The mark for the LAST child exists while the FIRST child is being killed.

    Opening each child's intent lazily inside the loop would leave the later
    children unmarked for part of the cascade, which is the same defect one
    node down. Sample the live scope keys at the first kill.
    """
    _create("rootcccc", env)
    _create("childccc", env, caller_id="rootcccc")
    _create("childddd", env, caller_id="rootcccc")

    seen_keys = []

    def under_lease(node_id, token, **_kw):
        env.kill(node_id)
        seen_keys.append(teardown_intent_service.active_teardown_scope_keys())
        return {"terminal_deleted": True, "resume_key": None}

    _arm_cascade(monkeypatch, under_lease)
    terminal_service.delete_terminal("rootcccc")

    assert seen_keys[0] >= {"rootcccc", "childccc", "childddd"}
    assert teardown_intent_service.active_teardown_scope_keys() == set()


def test_cascade_marks_are_released_even_when_a_node_delete_raises(env, monkeypatch):
    """The marks are TTL-bounded, but they must also unwind on the error path."""
    _create("rootdddd", env)
    _create("childeee", env, caller_id="rootdddd")

    def under_lease(node_id, token, **_kw):
        env.kill(node_id)
        raise RuntimeError("kill confirmation failed")

    _arm_cascade(monkeypatch, under_lease)

    with pytest.raises(RuntimeError, match="kill confirmation failed"):
        terminal_service.delete_terminal("rootdddd")

    assert teardown_intent_service.active_teardown_scope_keys() == set()
    with database.SessionLocal() as db:
        assert db.query(F218TeardownIntentModel).count() == 0
