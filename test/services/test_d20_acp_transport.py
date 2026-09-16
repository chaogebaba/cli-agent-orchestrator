"""D20 / AC-S1.10 — the terminal ``transport`` column and its consumers.

Four claims, each with its own mutant:

* the migrator is IDEMPOTENT under WAL (run twice -> same schema, no data change);
* a ``transport='acp'`` terminal is created with BOTH coordinates NULL and the
  table's CHECK still refuses a pane row without them;
* that terminal appears NON-ERROR in ``build_fleet``, and the branch that makes
  it so is on ``transport`` — deleting the branch turns the row ERROR;
* no named consumer tests a coordinate column for NULL.

The last one is AC-S1.10's grep, and it is the AC's real content.  ``NULL`` on a
coordinate is the ABSENCE of a pane, never the LOSS of one, and the whole
failure D20 guards against is a consumer that cannot tell those apart.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.core.transport import (
    Transport,
    is_acp_terminal,
    is_pane_terminal,
    transport_of,
)

# A pre-D20 terminals table: the two coordinates NOT NULL, no ``transport``
# column, and one trigger plus one index, so the rebuild has to preserve both.
_LEGACY_TERMINALS_DDL = """
CREATE TABLE terminals (
    id TEXT PRIMARY KEY,
    tmux_session TEXT NOT NULL,
    tmux_window TEXT NOT NULL,
    provider TEXT NOT NULL,
    agent_profile TEXT,
    worktree_info TEXT,
    lifecycle TEXT NOT NULL DEFAULT 'ephemeral'
        CHECK (lifecycle IN ('ephemeral','sticky')),
    lifecycle_generation INTEGER NOT NULL DEFAULT 0
)
"""

_LEGACY_INDEX = "CREATE INDEX ix_terminals_provider ON terminals (provider)"

_LEGACY_TRIGGER = (
    "CREATE TRIGGER terminals_worktree_info_immutable "
    "BEFORE UPDATE OF worktree_info ON terminals "
    "WHEN OLD.worktree_info IS NOT NULL AND NEW.worktree_info IS NOT OLD.worktree_info "
    "BEGIN SELECT RAISE(ABORT, 'worktree_info_immutable'); END"
)


def _schema(path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(str(path))
    try:
        return sorted(
            conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )
    finally:
        conn.close()


def _rows(path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(str(path))
    try:
        return sorted(conn.execute("SELECT * FROM terminals").fetchall())
    finally:
        conn.close()


@pytest.fixture
def legacy_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A WAL database holding one pre-D20 terminals table with one pane row."""
    path = tmp_path / "legacy-terminals.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_LEGACY_TERMINALS_DDL)
        conn.execute(_LEGACY_INDEX)
        conn.execute(_LEGACY_TRIGGER)
        conn.execute(
            "INSERT INTO terminals (id, tmux_session, tmux_window, provider, agent_profile) "
            "VALUES ('aaaa1111', 'cao-demo', 'w0', 'claude_code', 'developer')"
        )
    finally:
        conn.close()
    monkeypatch.setattr(db_mod, "_d20_database_file", lambda: path)
    return path


# ----------------------------------------------------------- the migrator (1)


def test_the_migrator_adds_the_column_and_nulls_the_coordinates(legacy_db: Path) -> None:
    db_mod._migrate_d20_acp_transport()
    conn = sqlite3.connect(str(legacy_db))
    try:
        info = {row[1]: row for row in conn.execute("PRAGMA table_info(terminals)")}
    finally:
        conn.close()
    assert "transport" in info
    # PRAGMA field 3 is ``notnull``; field 4 is ``dflt_value``.
    assert info["transport"][3] == 1, "transport is NOT NULL — it never needs a NULL reading"
    assert "'pane'" in str(info["transport"][4])
    assert info["tmux_session"][3] == 0
    assert info["tmux_window"][3] == 0


def test_the_pre_d20_row_is_backfilled_as_a_pane_terminal(legacy_db: Path) -> None:
    """Every row that existed before D20 IS a pane terminal; the default is the back-fill."""
    db_mod._migrate_d20_acp_transport()
    conn = sqlite3.connect(str(legacy_db))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM terminals WHERE id='aaaa1111'").fetchone()
    finally:
        conn.close()
    assert row["transport"] == "pane"
    assert row["tmux_session"] == "cao-demo"
    assert row["tmux_window"] == "w0"
    assert is_pane_terminal(dict(row))


def test_the_rebuild_preserves_the_index_and_the_trigger(legacy_db: Path) -> None:
    """A rebuild that silently drops a trigger is a rebuild that removed an invariant."""
    db_mod._migrate_d20_acp_transport()
    names = {name for _type, name, _sql in _schema(legacy_db)}
    assert "ix_terminals_provider" in names
    assert "terminals_worktree_info_immutable" in names


def test_the_migrator_is_idempotent_under_wal(legacy_db: Path) -> None:
    """AC-S1.10: run twice -> same schema, no data change.

    Asserted over ``sqlite_master`` and over the rows, not over a boolean the
    migration returns: "it did nothing" has to be observable in the database,
    because a second rebuild that produced an identical-looking schema while
    rewriting every row would still be a second rebuild.
    """
    db_mod._migrate_d20_acp_transport()
    schema_once, rows_once = _schema(legacy_db), _rows(legacy_db)
    db_mod._migrate_d20_acp_transport()
    assert _schema(legacy_db) == schema_once
    assert _rows(legacy_db) == rows_once


def test_a_third_run_is_still_a_no_op(legacy_db: Path) -> None:
    db_mod._migrate_d20_acp_transport()
    db_mod._migrate_d20_acp_transport()
    schema = _schema(legacy_db)
    db_mod._migrate_d20_acp_transport()
    assert _schema(legacy_db) == schema


def test_the_migrator_is_a_no_op_on_a_database_with_no_terminals_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty.db"
    sqlite3.connect(str(path)).close()
    monkeypatch.setattr(db_mod, "_d20_database_file", lambda: path)
    db_mod._migrate_d20_acp_transport()  # must not raise
    assert _schema(path) == []


# ------------------------------------------------- the row and its CHECKs (2)


def test_an_acp_terminal_is_created_with_both_coordinates_null(legacy_db: Path) -> None:
    db_mod._migrate_d20_acp_transport()
    conn = sqlite3.connect(str(legacy_db), isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO terminals (id, tmux_session, tmux_window, transport, provider) "
            "VALUES ('bbbb2222', NULL, NULL, 'acp', 'claude_code')"
        )
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM terminals WHERE id='bbbb2222'").fetchone()
    finally:
        conn.close()
    assert row["tmux_session"] is None
    assert row["tmux_window"] is None
    assert is_acp_terminal(dict(row))


def test_a_pane_terminal_still_may_not_drop_its_coordinates(legacy_db: Path) -> None:
    """The nullability is FOR ACP.  Making it unconditional would delete an invariant."""
    db_mod._migrate_d20_acp_transport()
    conn = sqlite3.connect(str(legacy_db), isolation_level=None)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO terminals (id, tmux_session, tmux_window, transport, provider) "
                "VALUES ('cccc3333', NULL, NULL, 'pane', 'claude_code')"
            )
    finally:
        conn.close()


def test_there_is_no_third_transport(legacy_db: Path) -> None:
    db_mod._migrate_d20_acp_transport()
    conn = sqlite3.connect(str(legacy_db), isolation_level=None)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO terminals (id, tmux_session, tmux_window, transport, provider) "
                "VALUES ('dddd4444', 's', 'w', 'herdr', 'claude_code')"
            )
    finally:
        conn.close()


def test_transport_of_is_total_and_defaults_to_pane() -> None:
    """A projection written before D20 has no key; it is a pane row, not an ACP one.

    Defaulting the other way would make every legacy dict look like an ACP seat
    and would SUPPRESS real pane-absence errors — a strictly worse failure than
    the one it would fix.
    """
    assert transport_of({}) is Transport.PANE
    assert transport_of({"transport": None}) is Transport.PANE
    assert transport_of({"transport": "nonsense"}) is Transport.PANE
    assert transport_of({"transport": "acp"}) is Transport.ACP

    class _Row:
        transport = "acp"

    assert transport_of(_Row()) is Transport.ACP


# ------------------------------------------------- the fleet projection (3)


def _fleet_row(transport: str) -> dict[str, Any]:
    return {
        "id": "eeee5555",
        "agent_profile": "developer",
        "provider": "claude_code",
        "tmux_window": None if transport == "acp" else "w0",
        "transport": transport,
        "caller_id": None,
        "last_active": None,
        "lifecycle": "ephemeral",
    }


def _wire_fleet(monkeypatch: pytest.MonkeyPatch, row: dict[str, Any]) -> Any:
    import cli_agent_orchestrator.services.fleet_service as fs
    from cli_agent_orchestrator.models.terminal import TerminalStatus

    monkeypatch.setattr(fs, "list_terminals_by_session", lambda _s: [row])

    class _Backend:
        def get_session_windows(self, _s: str) -> list[Any]:
            # The pane inventory is REAL and does not list the ACP row's window,
            # because an ACP terminal has none.  That is the whole point.
            return [{"name": "w0", "index": 0}]

    class _Observation:
        status = TerminalStatus.IDLE
        fusion_changed = False
        fusion_reason = None

    monkeypatch.setattr(fs, "get_backend", lambda: _Backend())
    monkeypatch.setattr(fs.status_monitor, "get_boundary_observation", lambda _tid: _Observation())
    monkeypatch.setattr(fs, "_compute_init_health", lambda _row, _now: "ok")
    return fs


def test_an_acp_terminal_is_not_error_in_the_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-S1.10's named failure: "any consumer stamps it ERROR"."""
    fs = _wire_fleet(monkeypatch, _fleet_row("acp"))
    rows = {r["id"]: r for r in fs.build_fleet("sess-d20")["terminals"]}
    assert rows["eeee5555"]["status"] == "idle"


def test_a_pane_terminal_with_a_missing_window_is_still_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control arm.  Without it the ACP test above passes on a projection
    that stopped detecting pane absence altogether, which would be a regression
    dressed as a feature."""
    row = _fleet_row("pane")
    row["tmux_window"] = "w-gone"
    fs = _wire_fleet(monkeypatch, row)
    rows = {r["id"]: r for r in fs.build_fleet("sess-d20")["terminals"]}
    assert rows["eeee5555"]["status"] == "error"


def test_mutant_the_fleet_branch_on_transport_is_load_bearing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTANT: make ``is_pane_terminal`` answer True for everything — i.e. delete
    the branch — and the healthy ACP seat goes ERROR.

    Patching the predicate at its use site is the smallest faithful expression
    of "the branch was removed": it leaves the inventory comparison exactly as it
    was and removes only the transport test.
    """
    fs = _wire_fleet(monkeypatch, _fleet_row("acp"))
    monkeypatch.setattr(fs, "is_pane_terminal", lambda _row: True)
    rows = {r["id"]: r for r in fs.build_fleet("sess-d20")["terminals"]}
    assert rows["eeee5555"]["status"] == "error", "the mutant must be RED"


def test_an_acp_parent_never_orphans_its_children(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second fleet branch: ``parent_dead`` reads the same inventory."""
    parent = _fleet_row("acp")
    parent["id"] = "ffff6666"
    child = _fleet_row("pane")
    child["id"] = "eeee5555"
    child["caller_id"] = "ffff6666"
    import cli_agent_orchestrator.services.fleet_service as fs
    from cli_agent_orchestrator.models.terminal import TerminalStatus

    monkeypatch.setattr(fs, "list_terminals_by_session", lambda _s: [parent, child])

    class _Backend:
        def get_session_windows(self, _s: str) -> list[Any]:
            return [{"name": "w0", "index": 0}]

    class _Observation:
        status = TerminalStatus.IDLE
        fusion_changed = False
        fusion_reason = None

    monkeypatch.setattr(fs, "get_backend", lambda: _Backend())
    monkeypatch.setattr(fs.status_monitor, "get_boundary_observation", lambda _tid: _Observation())
    monkeypatch.setattr(fs, "_compute_init_health", lambda _row, _now: "ok")
    rows = {r["id"]: r for r in fs.build_fleet("sess-d20")["terminals"]}
    assert rows["eeee5555"].get("orphan") is not True


# --------------------------------------------------------------- the grep (4)


_NAMED_CONSUMERS = (
    "services/fleet_service.py",
    "services/terminal_service.py",
    "backends/registry.py",
)

_FORBIDDEN = (
    "tmux_session is none",
    "tmux_window is none",
    "tmux_session is not none",
    "tmux_window is not none",
    "tmux_session is null",
    "tmux_window is null",
    "tmux_session is not null",
    "tmux_window is not null",
    "tmux_session isnot none",
    "tmux_window isnot none",
)

_NOISE = str.maketrans({ch: None for ch in "\"'[]()"})


def _normalise(line: str) -> str:
    """Strip the punctuation that separates a column name from its NULL test.

    ``row["tmux_window"] is None``, ``row.get('tmux_window') is None`` and
    ``t.tmux_window is None`` are one check written three ways, and a literal
    grep would catch whichever spelling the author happened to try first.
    """
    return " ".join(line.translate(_NOISE).lower().replace(".get", "").split())


def test_no_named_consumer_reads_a_null_coordinate_as_a_signal() -> None:
    """AC-S1.10's grep, as a test that runs on every build.

    The AC's fails-if is "reads NULL as a signal".  ``tmux_window IS NULL`` is
    true of every healthy ACP seat AND of nothing else, so any consumer that
    branches on it has written ``if this is an ACP seat`` in a vocabulary that
    cannot say so — and will keep being right for pane rows while being wrong
    for the whole new plane.
    """
    src = Path(db_mod.__file__).resolve().parents[1]
    offenders: list[str] = []
    for relative in _NAMED_CONSUMERS:
        path = src / relative
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # prose about the rule is not a violation of it
            lowered = _normalise(stripped)
            if any(needle in lowered for needle in _FORBIDDEN):
                offenders.append(f"{relative}:{number}: {stripped}")
    assert not offenders, "NULL coordinate checks (AC-S1.10):\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "sample",
    [
        "if row['tmux_window'] is None:",
        'if row["tmux_session"] is None:',
        "if terminal.tmux_window is not None:",
        'if row.get("tmux_window") is None:',
        "WHERE tmux_session IS NULL",
    ],
)
def test_the_grep_would_catch_a_real_violation(sample: str) -> None:
    """The guard above is only worth having if it can go RED, in every spelling."""
    normalised = _normalise(sample)
    assert any(needle in normalised for needle in _FORBIDDEN), normalised


def test_the_grep_does_not_fire_on_a_transport_branch() -> None:
    """The control: the CORRECT code must not trip its own guard."""
    for sample in (
        "if is_pane_terminal(row) and row['tmux_window'] not in windows:",
        'if row["transport"] == "acp":',
    ):
        normalised = _normalise(sample)
        assert not any(needle in normalised for needle in _FORBIDDEN), normalised


# ------------------------------------------------- the backend lookup (D20)


def test_an_acp_terminal_has_no_pane_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli_agent_orchestrator.backends import registry

    sentinel = object()
    monkeypatch.setattr(registry, "get_backend", lambda: sentinel)
    assert registry.backend_for_terminal({"transport": "acp"}) is None
    assert registry.backend_for_terminal({"transport": "pane"}) is sentinel
    assert registry.backend_for_terminal({}) is sentinel
