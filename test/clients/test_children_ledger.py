"""F568 D12a children-ledger DB mutators — arithmetic, idempotence, staleness.

The ledger lives at the free-form ``metadata_json["children"]`` top-level key —
the SAME list D12d's frozen READ side (``pane_liveness._children_count_from_metadata``)
counts. These tests pin the register/decrement lifecycle, the no-double-count
idempotence, the FIFO release, the staleness prune, and the sibling-key
preservation (the ledger write must not clobber other metadata).
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


def _children(terminal_id="t1"):
    meta = get_terminal_metadata(terminal_id)
    assert meta is not None
    free_form = meta.get("metadata") or {}
    return free_form.get("children") or []


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
    # Empty ledger drops the key entirely (does not leave [] behind).
    assert _children() == []


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
    assert meta["note"] == "keep me"
    assert [e["id"] for e in meta["children"]] == ["pre", "c1"]


def test_release_preserves_sibling_free_form_keys(db_env):
    import time as _time

    now = _time.time()
    _seed(metadata={"note": "keep me", "children": [{"id": "c1", "started_at": now - 1.0}]})
    release_terminal_child("t1", "c1")
    meta = get_terminal_metadata("t1")["metadata"]
    assert meta["note"] == "keep me"
    assert "children" not in meta


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
