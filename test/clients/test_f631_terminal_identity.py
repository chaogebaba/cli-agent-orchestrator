"""F631 slice 1 — the durable identity row and the create/reap lifecycle.

Blueprint: ``orchestrator/blueprints/f631-terminal-identity-registry.md``
(DRAFT r6). Slice 1 covers D1 (a NEW ``terminal_identity`` table,
``provider_sessions`` left alone), D2 (the two-value ``lifecycle`` column),
D10 (the additive migration) and §3's create/reap write ownership, including
D4's reap half — ``delete_terminal`` returns the resume key.

Arms: AC1, AC2, AC14, AC15, plus D2's CHECK and D4's reap-side return, plus
the two fault-injection arms that hold §3's atomicity guarantee when the
identity write itself FAILS — the case a swallowed exception hid from M10, which
can only observe the outer abort path. Those two are **state-only by
construction** (r3): they make no assertion about which exception propagates, or
whether one does, and their fault is injected PRE-FLUSH so the swallowed-success
state is actually reachable. Every
assertion runs against the real SQLAlchemy tables / real DB operations, never a
mocked surface. The ``db_env`` fixture mirrors
``test/clients/test_f642_delivery_ledger.py``.

Out of slice (no D-row/AC here): D3's provider-typed capture and the
eligibility projection, D4's ``resolve_base`` branches, the D5/D6/D7/D8/D12 pin
plane, D9's box lane and D11's persona-home claim re-key.
"""

import sqlite3
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    ProviderSessionModel,
    TerminalIdentityModel,
    TerminalModel,
    _migrate_f631_terminal_identity,
    create_terminal,
    delete_terminal_and_warm_intent,
    get_terminal_identity,
    get_terminal_metadata,
    list_ready_provider_sessions,
    list_terminals_by_session,
)

SESSION = "cao-f631"


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


def _make_lane(terminal_id="lane0001", *, provider="codex", uuid_value="uuid-lane0001"):
    return create_terminal(
        terminal_id,
        SESSION,
        f"worker-{terminal_id}",
        provider,
        agent_profile="codex_dev",
        working_directory="/home/chao/repo",
        provider_session_id=uuid_value,
    )


# ── AC1 (D1/D2): registration at create ─────────────────────────────────────


def test_ac1_create_writes_a_live_identity_row(db_env):
    """Creating a worker terminal writes its identity row immediately."""
    _make_lane()

    row = get_terminal_identity("lane0001")
    assert row is not None, "no identity row was written at create"
    assert row["lifecycle"] == "live"
    assert row["reaped_at"] is None
    assert row["created_at"] is not None


def test_ac1_identity_row_carries_what_the_resume_consumer_dereferences(db_env):
    """The column set is derived from what the consumer reads off a resolved row.

    ``base_name`` is the value exposed as ``row["name"]``, dereferenced
    unconditionally on every live resume path — for an identity row it is the
    terminal_id itself (§2).
    """
    _make_lane()

    row = get_terminal_identity("lane0001")
    assert row["terminal_id"] == "lane0001"
    assert row["base_name"] == "lane0001"
    assert row["provider"] == "codex"
    assert row["agent_profile"] == "codex_dev"
    assert row["cwd"] == "/home/chao/repo"
    assert row["session_name"] == SESSION
    assert row["provider_session_id"] == "uuid-lane0001"


def test_ac1_create_leaves_provider_sessions_untouched(db_env):
    """AC1: ``provider_sessions`` is unchanged — the lane is NOT a fork base."""
    _make_lane()
    _make_lane("lane0002", uuid_value="uuid-lane0002")

    with db_env() as db:
        assert db.query(ProviderSessionModel).count() == 0
    assert list_ready_provider_sessions() == []


def test_ac1_mutant_provider_sessions_rejects_the_identity_row(db_env):
    """AC1's mutant, made executable: write the row into ``provider_sessions``.

    B1 said the existing table "already carries every column this needs". It
    does not — three constraints reject an at-create identity row, and each is
    asserted separately so the arm names WHICH constraint fires.
    """
    # (1) session_uuid is NOT NULL — an at-create row has no uuid yet.
    with db_env() as db:
        db.add(
            ProviderSessionModel(
                name="lane0001",
                provider="codex",
                session_uuid=None,
                cwd="/home/chao/repo",
                agent_profile="codex_dev",
                status="ready",
                kind="base",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    # (2) status is CHECK-constrained to ready|superseded|retired.
    with db_env() as db:
        db.add(
            ProviderSessionModel(
                name="lane0001",
                provider="codex",
                session_uuid="uuid-lane0001",
                cwd="/home/chao/repo",
                agent_profile="codex_dev",
                status="live",
                kind="base",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    # (3) kind is CHECK-constrained to base|anchor.
    with db_env() as db:
        db.add(
            ProviderSessionModel(
                name="lane0001",
                provider="codex",
                session_uuid="uuid-lane0001",
                cwd="/home/chao/repo",
                agent_profile="codex_dev",
                status="ready",
                kind="identity",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_ac1_identity_row_is_written_in_the_terminals_transaction(db_env):
    """§3: a failing create leaves NO identity row behind.

    ``create_terminal`` raises ``barrier_owner_not_found`` when a dispatch
    barrier names no caller — after the identity insert, before the commit. If
    the identity row were written in its own transaction it would survive; in
    the terminals transaction it rolls back with everything else.
    """
    with pytest.raises(ValueError, match="barrier_owner_not_found"):
        create_terminal(
            "lane0003",
            SESSION,
            "worker-lane0003",
            "codex",
            caller_id=None,
            dispatch_barrier={"mode": "all"},
        )

    assert get_terminal_identity("lane0003") is None
    with db_env() as db:
        assert db.query(TerminalModel).filter_by(id="lane0003").one_or_none() is None


@contextmanager
def _identity_row_construction_fails():
    """PRE-FLUSH fault on the create side: the model constructor raises.

    The injection point matters more than the fault (r3). A mapper
    ``before_insert`` listener — what r2 used — raises *during* ``Session.flush()``,
    which poisons the transaction; the outer commit then fails on its own and the
    forbidden state is never reachable, so restoring the swallow could not be
    caught by durable state at all. This fault is pure Python, raised while
    building the row and before any DB interaction, so the caller's transaction
    stays healthy and a swallowed failure really does commit the terminals row
    alone — the exact state the r1 probe recorded as ``1, 0``.

    ``terminal_id`` is the real instrumented attribute so the helper's
    existence query is unaffected; only construction fails.
    """
    real = database.TerminalIdentityModel

    class _RaisingIdentity:
        terminal_id = real.terminal_id

        def __init__(self, **kwargs):
            raise RuntimeError("identity_row_construction_failed")

    setattr(database, "TerminalIdentityModel", _RaisingIdentity)
    try:
        yield
    finally:
        setattr(database, "TerminalIdentityModel", real)


@contextmanager
def _identity_retirement_fails():
    """PRE-FLUSH fault on the reap side: assigning ``lifecycle`` raises.

    An attribute ``set`` event fires synchronously in Python during
    ``row.lifecycle = "reaped"``, before any flush, so — as above — the
    transaction is still usable and a swallowed failure really does commit the
    hard delete while leaving the identity row live.
    """

    def boom(target, value, oldvalue, initiator):
        raise RuntimeError("identity_retirement_failed")

    event.listen(TerminalIdentityModel.lifecycle, "set", boom)
    try:
        yield
    finally:
        event.remove(TerminalIdentityModel.lifecycle, "set", boom)


def test_a_failed_identity_insert_aborts_the_whole_create(db_env):
    """§3/AC1: registration failure must roll the terminal back, not fail open.

    STATE-ONLY BY CONSTRUCTION (r3). This arm makes no assertion whatsoever
    about which exception propagates, or whether one propagates at all — the
    call is wrapped in a bare catch and the verdict is read entirely off the
    database afterwards. There is therefore no exception matcher to broaden: the
    only thing that can turn this arm red is the forbidden durable state.

    The guarantee §3 makes is not call ordering — M10 already covers the outer
    abort path — it is that a laptop-created terminal is NEVER unregistered. A
    swallowed failure defeats that without ever reaching the outer path: the
    transaction is never told anything went wrong and commits one terminals row
    with zero identity rows.
    """
    with _identity_row_construction_fails():
        try:
            _make_lane("lane0008")
        except Exception:
            pass  # deliberately unexamined — see the docstring

    with db_env() as db:
        state = (db.query(TerminalModel).count(), db.query(TerminalIdentityModel).count())
    assert state == (0, 0), f"F631-N1-STATE: terminals,identities = {state}, want (0, 0)"


def test_a_failed_identity_retirement_aborts_the_whole_reap(db_env):
    """D2/D4/§3/AC2: retirement failure must abort the delete, not fail open.

    STATE-ONLY BY CONSTRUCTION (r3), for the same reason as the create-side arm.

    Failing open here is worse than losing the key: the terminals row is hard
    deleted while the durable identity stays ``live`` with no ``reaped_at`` — a
    reaped lane that reads as live. The discriminator is therefore the TERMINALS
    row (deleted when the failure is swallowed, alive when it propagates); the
    identity row reads ``live``/unstamped either way, which is precisely why
    asserting only on the identity row would prove nothing.
    """
    _make_lane()

    with _identity_retirement_fails():
        try:
            delete_terminal_and_warm_intent("lane0001")
        except Exception:
            pass  # deliberately unexamined — see the docstring

    with db_env() as db:
        survived = db.query(TerminalModel).filter_by(id="lane0001").one_or_none() is not None
    assert survived, "F631-N2-STATE: the terminals row was hard-deleted despite a failed retirement"
    row = get_terminal_identity("lane0001")
    assert (row["lifecycle"], row["reaped_at"]) == ("live", None), (
        f"F631-N2-STATE: identity is {row['lifecycle']}/{row['reaped_at']}, want live/None"
    )
    # …and the real key was never silently discarded — a second, clean reap
    # still returns it.
    assert delete_terminal_and_warm_intent("lane0001")["resume_key"] == "uuid-lane0001"


def test_a_failed_identity_write_propagates_rather_than_returning(db_env):
    """Companion to the two arms above — NOT one of them.

    The blocker arms deliberately say nothing about exceptions, so this records
    the propagation property separately. It is documented as unable to kill the
    catch-all mutation (under the swallow nothing propagates and this test fails
    for a non-state reason), which is exactly why it is kept apart from them.
    """
    with _identity_row_construction_fails():
        with pytest.raises(Exception):
            _make_lane("lane0010")

    _make_lane("lane0011", uuid_value="uuid-lane0011")
    with _identity_retirement_fails():
        with pytest.raises(Exception):
            delete_terminal_and_warm_intent("lane0011")


def test_a_pre_registry_lane_is_control_flow_not_a_tolerated_exception(db_env):
    """Negative control for both arms above.

    Removing the catch-alls must not remove the one case the record DOES
    support: a terminal with no identity row reaps cleanly, returning None. That
    path is an ``one_or_none()`` early return, so it survives having no handler.
    """
    with db_env() as db:
        db.add(
            TerminalModel(
                id="lane0009",
                tmux_session=SESSION,
                tmux_window="worker-lane0009",
                provider="codex",
            )
        )
        db.commit()

    result = delete_terminal_and_warm_intent("lane0009")
    assert result == {"terminal_deleted": True, "intent_deleted": False, "resume_key": None}


def test_nothing_fabricates_a_provider_session_id(db_env):
    """D3: capture is provider-typed; create records NULL when there is none."""
    create_terminal("lane0004", SESSION, "worker-lane0004", "kiro_cli")

    row = get_terminal_identity("lane0004")
    assert row is not None
    assert row["provider_session_id"] is None


# ── AC2 (D1/D4): the identity outlives the terminal ─────────────────────────


def test_ac2_identity_row_survives_the_reap(db_env):
    """The terminals row is hard-deleted; the identity row is retired, not gone."""
    _make_lane()
    created_at = get_terminal_identity("lane0001")["created_at"]

    result = delete_terminal_and_warm_intent("lane0001")
    assert result["terminal_deleted"] is True

    with db_env() as db:
        assert db.query(TerminalModel).filter_by(id="lane0001").one_or_none() is None

    row = get_terminal_identity("lane0001")
    assert row is not None, "the identity row did not outlive the terminal"
    assert row["lifecycle"] == "reaped"
    assert row["reaped_at"] is not None
    assert row["created_at"] == created_at
    # The resume key survives the reap — that is the point of the row.
    assert row["provider_session_id"] == "uuid-lane0001"
    assert row["base_name"] == "lane0001"


def test_ac2_reap_does_not_disturb_a_sibling_lane(db_env):
    """Negative control: only the reaped lane's identity row changes state."""
    _make_lane()
    _make_lane("lane0002", uuid_value="uuid-lane0002")

    delete_terminal_and_warm_intent("lane0001")

    sibling = get_terminal_identity("lane0002")
    assert sibling["lifecycle"] == "live"
    assert sibling["reaped_at"] is None


# ── D4 (reap half): delete_terminal returns the resume key ──────────────────


def test_reap_returns_the_resume_key(db_env):
    """§1: the delete result used to carry no resume key at all."""
    _make_lane()

    result = delete_terminal_and_warm_intent("lane0001")
    assert result["resume_key"] == "uuid-lane0001"


def test_reap_returns_none_when_the_lane_has_no_provider_session(db_env):
    """A lane whose provider never minted an id reaps to a None key, not a lie."""
    create_terminal("lane0004", SESSION, "worker-lane0004", "kiro_cli")

    result = delete_terminal_and_warm_intent("lane0004")
    assert result["resume_key"] is None
    assert get_terminal_identity("lane0004")["lifecycle"] == "reaped"


def test_reap_of_a_pre_registry_lane_returns_none_and_still_deletes(db_env):
    """D10: a terminal with no identity row reaps cleanly, with no key."""
    with db_env() as db:
        db.add(
            TerminalModel(
                id="lane0005",
                tmux_session=SESSION,
                tmux_window="worker-lane0005",
                provider="codex",
            )
        )
        db.commit()

    result = delete_terminal_and_warm_intent("lane0005")
    assert result["terminal_deleted"] is True
    assert result["resume_key"] is None
    assert get_terminal_identity("lane0005") is None


# ── AC14 (D1): no resurrection ──────────────────────────────────────────────


def test_ac14_reaped_lane_is_invisible_to_live_terminal_projections(db_env):
    """A ``reaped`` identity row never appears as a lane (§4)."""
    _make_lane()
    _make_lane("lane0002", uuid_value="uuid-lane0002")

    delete_terminal_and_warm_intent("lane0001")

    live_ids = {row["id"] for row in list_terminals_by_session(SESSION)}
    assert live_ids == {"lane0002"}
    assert get_terminal_metadata("lane0001") is None
    # …while the identity row is still there to be resumed by.
    assert get_terminal_identity("lane0001")["lifecycle"] == "reaped"


# ── D2: the lifecycle column is two-valued ──────────────────────────────────


def test_d2_lifecycle_check_rejects_a_third_value(db_env):
    """The CHECK is real, and it collides with nothing (it is a new table)."""
    with db_env() as db:
        db.add(
            TerminalIdentityModel(
                terminal_id="lane0006",
                provider="codex",
                base_name="lane0006",
                lifecycle="migrated",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_d2_terminals_lifecycle_vocabulary_is_unaffected(db_env):
    """Negative control: ``terminals.lifecycle`` still means ephemeral|sticky.

    The two columns share a name and nothing else; F631's CHECK must not have
    been added to the wrong table.
    """
    create_terminal("lane0007", SESSION, "worker-lane0007", "codex", lifecycle="sticky")

    with db_env() as db:
        assert db.query(TerminalModel).filter_by(id="lane0007").one().lifecycle == "sticky"
    assert get_terminal_identity("lane0007")["lifecycle"] == "live"


# ── AC15 (D10): the migration is additive ───────────────────────────────────


def _schema_row(conn, name):
    return conn.execute(
        "SELECT sql, rootpage FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()


def test_ac15_migration_creates_the_table_and_rebuilds_nothing(tmp_path, monkeypatch):
    """provider_sessions' ``sql`` text AND ``rootpage`` are unchanged.

    In SQLite a rebuild rewrites the table, so a moved rootpage is the concrete
    falsifier for "no rebuild" — a judgement call made executable.
    """
    db_path = tmp_path / "prod_copy.db"
    file_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(file_engine)
    file_engine.dispose()

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DROP TABLE terminal_identity")  # simulate a pre-F631 database
        before_ps = _schema_row(conn, "provider_sessions")
        before_terminals = _schema_row(conn, "terminals")
        before_index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='uq_provider_sessions_ready'"
        ).fetchone()
        conn.execute(
            "INSERT INTO provider_sessions "
            "(name, provider, session_uuid, cwd, agent_profile, dirty_hashes, status, kind) "
            "VALUES ('base-a', 'codex', 'uuid-a', '/repo', 'codex_dev', '{}', 'ready', 'base')"
        )

    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_f631_terminal_identity()

    with sqlite3.connect(str(db_path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "terminal_identity" in tables
        assert _schema_row(conn, "provider_sessions") == before_ps
        assert _schema_row(conn, "terminals") == before_terminals
        assert (
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='uq_provider_sessions_ready'"
            ).fetchone()
            == before_index
        )
        assert conn.execute("SELECT count(*) FROM provider_sessions").fetchone()[0] == 1

    # Idempotent — a second boot is a no-op.
    _migrate_f631_terminal_identity()
    with sqlite3.connect(str(db_path)) as conn:
        assert _schema_row(conn, "provider_sessions") == before_ps


def test_ac15_migrated_table_matches_the_model(tmp_path, monkeypatch):
    """The migrator's DDL and ``Base.metadata.create_all`` must agree.

    A fresh install gets the table from the model; an existing DB gets it from
    the migrator. If the two drift, half the fleet runs a different schema.
    """
    migrated = tmp_path / "migrated.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", migrated, raising=True)
    _migrate_f631_terminal_identity()

    fresh = tmp_path / "fresh.db"
    fresh_engine = create_engine(f"sqlite:///{fresh}")
    Base.metadata.create_all(fresh_engine)
    fresh_engine.dispose()

    def _columns(path):
        with sqlite3.connect(str(path)) as conn:
            return {
                (r[1], r[2].upper(), r[3], r[5])  # name, type, notnull, pk
                for r in conn.execute("PRAGMA table_info(terminal_identity)")
            }

    assert _columns(migrated) == _columns(fresh)

    # The CHECK and the resume-key index survive the migrator too.
    with sqlite3.connect(str(migrated)) as conn:
        conn.execute(
            "INSERT INTO terminal_identity (terminal_id, provider, base_name, lifecycle) "
            "VALUES ('x', 'codex', 'x', 'live')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO terminal_identity (terminal_id, provider, base_name, lifecycle) "
                "VALUES ('y', 'codex', 'y', 'migrated')"
            )
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(terminal_identity)")}
        assert "ix_terminal_identity_provider_session_id" in indexes
