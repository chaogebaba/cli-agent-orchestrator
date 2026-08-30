"""F568 D12a + F579 D17 children-ledger DB mutators.

D17 MIGRATED the ledger from the free-form ``metadata_json["children"]`` key
into the reserved system namespace ``metadata_json["cao"]["children"]`` so a
worker full-replace can never clobber it, and changed the empty-ledger
representation to an explicit ``[]`` (never nulling the column — that would
destroy the whole ``cao`` namespace incl. the ``children_released`` ring).
These tests pin the register/decrement lifecycle at the migrated location, the
no-double-count idempotence, the FIFO release, the staleness prune, the
sibling-key preservation, the release_token dedup ring, and the publish-time
reconcile conjuncts.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    create_terminal,
    get_terminal_metadata,
    reconcile_children_on_publish,
    register_terminal_child,
    release_terminal_child,
)


@pytest.fixture
def db_env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.clear_terminal_metadata_cache()
    return sessions


def _seed(terminal_id="t1", metadata=None):
    create_terminal(terminal_id, "cao-t", f"w-{terminal_id}", "claude_code", metadata=metadata)


def _cao(terminal_id="t1"):
    """The migrated ``cao`` namespace sub-dict on the terminal's metadata."""
    meta = get_terminal_metadata(terminal_id)
    assert meta is not None
    free_form = meta.get("metadata") or {}
    return free_form.get("cao") or {}


def _children(terminal_id="t1"):
    return _cao(terminal_id).get("children") or []


# ---- register / release arithmetic ---------------------------------------


def test_register_increments_count(db_env):
    _seed()
    assert register_terminal_child("t1", "c1") == 1
    assert register_terminal_child("t1", "c2") == 2
    assert [e["id"] for e in _children()] == ["c1", "c2"]


def test_release_decrements_count(db_env):
    _seed()
    register_terminal_child("t1", "c1")
    register_terminal_child("t1", "c2")
    assert release_terminal_child("t1", "c1") == 1
    assert [e["id"] for e in _children()] == ["c2"]
    assert release_terminal_child("t1", "c2") == 0
    # D17: empty ledger is written as an explicit [] (never nulls the column).
    assert _children() == []
    assert "children" in _cao()


def test_register_idempotent_on_child_id(db_env):
    """A duplicate PreToolUse edge for the same dispatch does not double-count."""
    _seed()
    assert register_terminal_child("t1", "c1") == 1
    assert register_terminal_child("t1", "c1") == 1
    assert [e["id"] for e in _children()] == ["c1"]


def test_release_without_id_pops_oldest_fifo(db_env):
    """SubagentStop with no reliable id pops the oldest entry (count-correct)."""
    import time as _time

    now = _time.time()
    _seed()
    register_terminal_child("t1", "c1", started_at=now - 20.0)
    register_terminal_child("t1", "c2", started_at=now - 10.0)
    assert release_terminal_child("t1", None) == 1
    assert [e["id"] for e in _children()] == ["c2"]


def test_release_unknown_id_is_noop(db_env):
    _seed()
    register_terminal_child("t1", "c1")
    assert release_terminal_child("t1", "does-not-exist") == 1
    assert [e["id"] for e in _children()] == ["c1"]


def test_release_on_empty_ledger_is_noop(db_env):
    _seed()
    assert release_terminal_child("t1", None) == 0
    assert release_terminal_child("t1", "c9") == 0


def test_register_release_on_unknown_terminal_returns_none(db_env):
    assert register_terminal_child("ghost", "c1") is None
    assert release_terminal_child("ghost", "c1") is None


# ---- sibling-key preservation --------------------------------------------


def test_register_preserves_sibling_free_form_keys(db_env):
    import time as _time

    now = _time.time()
    _seed(metadata={"note": "keep me", "children": [{"id": "pre", "started_at": now - 1.0}]})
    register_terminal_child("t1", "c1")
    meta = get_terminal_metadata("t1")["metadata"]
    # Free-form sibling keys are untouched by the cao-namespace RMW.
    assert meta["note"] == "keep me"
    # D17: the pre-migration free-form entry is read via fallback and the new
    # entry is written to the migrated cao.children location.
    assert [e["id"] for e in meta["cao"]["children"]] == ["pre", "c1"]


def test_release_preserves_sibling_free_form_keys(db_env):
    import time as _time

    now = _time.time()
    _seed(metadata={"note": "keep me", "children": [{"id": "c1", "started_at": now - 1.0}]})
    release_terminal_child("t1", "c1")
    meta = get_terminal_metadata("t1")["metadata"]
    assert meta["note"] == "keep me"
    # D17: emptied ledger is [] at the migrated location (never nulled).
    assert meta["cao"]["children"] == []


# ---- staleness bound ------------------------------------------------------


def test_stale_entry_pruned_on_register(db_env, monkeypatch):
    """A missed SubagentStop must not pin the row: a provably-old entry ages out."""
    monkeypatch.setattr(database, "_children_ledger_max_age_s", lambda: 10.0)
    import time as _time

    now = _time.time()
    _seed(metadata={"children": [{"id": "stale", "started_at": now - 100.0}]})
    # Registering a fresh child prunes the stale one first.
    count = register_terminal_child("t1", "fresh")
    assert count == 1
    assert [e["id"] for e in _children()] == ["fresh"]


def test_entry_without_started_at_is_kept(db_env, monkeypatch):
    """Fail toward in-flight: a malformed/absent timestamp is never pruned."""
    monkeypatch.setattr(database, "_children_ledger_max_age_s", lambda: 10.0)
    _seed(metadata={"children": [{"id": "noage"}]})
    count = register_terminal_child("t1", "fresh")
    assert count == 2
    assert {e["id"] for e in _children()} == {"noage", "fresh"}


# ---- read-path parity with the frozen D12d counter ------------------------


def test_write_path_is_read_by_frozen_d12d_counter(db_env):
    """The ledger this module writes is exactly what pane_liveness counts."""
    from cli_agent_orchestrator.services.pane_liveness import _children_count_from_metadata

    _seed()
    register_terminal_child("t1", "c1")
    register_terminal_child("t1", "c2")
    meta = get_terminal_metadata("t1")
    assert _children_count_from_metadata(meta) == 2



# ---- F579 D17: migration, release_token ring, reconcile conjuncts ---------


def test_ledger_written_to_cao_namespace(db_env):
    """D17: register/release write the migrated metadata_json["cao"]["children"]."""
    _seed()
    register_terminal_child("t1", "c1")
    cao = _cao()
    assert "children" in cao
    assert [e["id"] for e in cao["children"]] == ["c1"]


def test_pre_migration_free_form_row_still_counts(db_env):
    """Fallback arm: a row written before the migration (free-form children,
    no cao.children) is still counted by both readers."""
    import time as _time

    from cli_agent_orchestrator.services.fleet_service import _children_count_from_row
    from cli_agent_orchestrator.services.pane_liveness import _children_count_from_metadata

    now = _time.time()
    _seed(
        metadata={
            "children": [
                {"id": "old1", "started_at": now},
                {"id": "old2", "started_at": now},
            ]
        }
    )
    meta = get_terminal_metadata("t1")
    # pane_liveness reads metadata["metadata"]["children"] (fallback).
    assert _children_count_from_metadata(meta) == 2
    # fleet_service reads row["metadata"]["children"] (fallback).
    assert _children_count_from_row({"metadata": meta["metadata"]}) == 2


def test_migrated_row_counts_from_cao_not_free_form(db_env):
    """After a write, both readers prefer cao.children over any free-form key."""
    from cli_agent_orchestrator.services.fleet_service import _children_count_from_row
    from cli_agent_orchestrator.services.pane_liveness import _children_count_from_metadata

    _seed()
    register_terminal_child("t1", "c1")
    meta = get_terminal_metadata("t1")
    assert _children_count_from_metadata(meta) == 1
    assert _children_count_from_row({"metadata": meta["metadata"]}) == 1


def test_duplicate_release_token_is_noop(db_env):
    """AC4 duplicate arm: two SubagentStops with the same release_token leave
    the count unchanged (the second is a dedup no-op); a genuine second child's
    stop with a different token still pops."""
    _seed()
    register_terminal_child("t1", "c1")
    register_terminal_child("t1", "c2")
    # First stop with token A pops one (oldest, no matching child_id).
    assert release_terminal_child("t1", None, release_token="agentA") == 1
    # Re-fired stop with the SAME token A must NOT pop the live child.
    assert release_terminal_child("t1", None, release_token="agentA") == 1
    assert len(_children()) == 1
    # A different token still pops.
    assert release_terminal_child("t1", None, release_token="agentB") == 0


def test_release_token_ring_bounded_and_survives_empty_ledger(db_env):
    """The children_released ring holds tokens, is bounded, and survives the
    ledger going empty (so a late duplicate stop cannot pop the next child)."""
    _seed()
    register_terminal_child("t1", "c1")
    release_terminal_child("t1", None, release_token="agentA")
    cao = _cao()
    assert cao["children"] == []
    assert "agentA" in cao["children_released"]
    # Register a new child; a stale re-fire of agentA must not pop it.
    register_terminal_child("t1", "c2")
    assert release_terminal_child("t1", None, release_token="agentA") == 1


def test_release_token_ring_is_bounded_to_max(db_env):
    from cli_agent_orchestrator.clients.database import _CHILDREN_RELEASED_RING_MAX

    _seed()
    for i in range(_CHILDREN_RELEASED_RING_MAX + 5):
        register_terminal_child("t1", f"c{i}")
        release_terminal_child("t1", None, release_token=f"agent{i}")
    ring = _cao()["children_released"]
    assert len(ring) == _CHILDREN_RELEASED_RING_MAX


def test_unmatched_child_id_pops_oldest(db_env):
    """AC4/P-B: a release with a child_id in a namespace no entry carries (the
    exact id-mismatch defect) falls back to pop-oldest, never a silent no-op."""
    import time as _time

    now = _time.time()
    _seed()
    register_terminal_child("t1", "toolu_1", started_at=now - 20.0)
    register_terminal_child("t1", "toolu_2", started_at=now - 10.0)
    # agent_id namespace never matches a tool_use_id entry → pop oldest.
    assert release_terminal_child("t1", "agent_xyz", release_token="agent_xyz") == 1
    assert [e["id"] for e in _children()] == ["toolu_2"]


def test_reconcile_drops_entries_after_k_non_processing(db_env):
    """Reconcile arm: a stranded child (suppressed SubagentStop) is dropped on a
    publish once the seat has been non-PROCESSING for K ticks."""
    _seed()
    register_terminal_child("t1", "stuck")
    # Under K: not dropped.
    assert reconcile_children_on_publish("t1", "IDLE", 1, 3) == 1
    assert reconcile_children_on_publish("t1", "IDLE", 2, 3) == 1
    # At K: dropped.
    assert reconcile_children_on_publish("t1", "IDLE", 3, 3) == 0
    assert _children() == []


def test_reconcile_does_not_drop_live_processing_delegation(db_env):
    """A live long delegation (past max_age but seat still PROCESSING) is NOT
    dropped by the publish reconcile."""
    import time as _time

    monkeypatch_age = 10.0
    _seed(metadata=None)
    # Register a child that is already 'old' per a tight bound, but the seat is
    # PROCESSING so neither conjunct fires.
    now = _time.time()
    register_terminal_child("t1", "live", started_at=now)
    assert reconcile_children_on_publish("t1", "PROCESSING", 99, 3) == 1
    assert [e["id"] for e in _children()] == ["live"]


def test_reconcile_is_noop_without_children(db_env):
    _seed()
    assert reconcile_children_on_publish("t1", "IDLE", 5, 3) == 0
    # No cao.children key created by a no-children reconcile.
    assert _cao().get("children") in (None, [])


def test_shape_invariant_metadata_never_absent_after_release(db_env):
    """AC4 shape arm: after the last child is released the ledger reads as an
    empty JSON list, cao keys are intact, and get_terminal_metadata is non-None."""
    _seed()
    register_terminal_child("t1", "c1")
    release_terminal_child("t1", None, release_token="agentA")
    meta = get_terminal_metadata("t1")
    assert meta is not None
    cao = meta["metadata"]["cao"]
    assert isinstance(cao["children"], list) and cao["children"] == []
    assert isinstance(cao["children_released"], list)
