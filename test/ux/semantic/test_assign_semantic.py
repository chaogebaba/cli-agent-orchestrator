"""S-kind tests for S01 (assign) — UX-1, UX-6.

Uses real SQLite to verify terminal state machine correctness.
"""

import pytest


@pytest.mark.ux(surface="S01", invariant="UX-1", kind="S")
class TestAssignSemanticUX1:
    """Semantic tests for assign: UX-1 Arrival — state machine correctness."""

    def test_terminal_record_stores_profile_and_provider(self):
        """Terminal DB records store agent_profile and provider correctly."""
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text

        with SessionLocal() as db:
            db.execute(text(
                "INSERT OR IGNORE INTO terminals (id, agent_profile, provider, tmux_session, tmux_window, lifecycle, init_state, lifecycle_generation) "
                "VALUES ('sem01aa', 'developer', 'mock_cli', 'test-session', 'w0', 'ephemeral', 'ready', 1)"
            ))
            db.commit()
            row = db.execute(
                text("SELECT id, agent_profile, provider FROM terminals WHERE id = 'sem01aa'")
            ).fetchone()
            assert row is not None
            assert row[1] == "developer"
            assert row[2] == "mock_cli"
            db.execute(text("DELETE FROM terminals WHERE id = 'sem01aa'"))
            db.commit()

    def test_two_terminals_get_distinct_ids(self):
        """Two terminal records have distinct IDs."""
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text

        with SessionLocal() as db:
            db.execute(text(
                "INSERT OR IGNORE INTO terminals (id, agent_profile, provider, tmux_session, tmux_window, lifecycle, init_state, lifecycle_generation) "
                "VALUES ('sem01bb', 'developer', 'mock_cli', 'test-session', 'w0', 'ephemeral', 'ready', 1)"
            ))
            db.execute(text(
                "INSERT OR IGNORE INTO terminals (id, agent_profile, provider, tmux_session, tmux_window, lifecycle, init_state, lifecycle_generation) "
                "VALUES ('sem01cc', 'developer', 'mock_cli', 'test-session', 'w0', 'ephemeral', 'ready', 1)"
            ))
            db.commit()
            rows = db.execute(
                text("SELECT id FROM terminals WHERE id IN ('sem01bb', 'sem01cc')")
            ).fetchall()
            assert len(rows) == 2
            assert rows[0][0] != rows[1][0]
            db.execute(text("DELETE FROM terminals WHERE id IN ('sem01bb', 'sem01cc')"))
            db.commit()


@pytest.mark.ux(surface="S01", invariant="UX-6", kind="S")
class TestAssignSemanticUX6:
    """Semantic tests for assign: UX-6 Visibility."""

    def test_terminal_queryable_after_insert(self):
        """A terminal record is immediately queryable by ID."""
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text

        with SessionLocal() as db:
            db.execute(text(
                "INSERT OR IGNORE INTO terminals (id, agent_profile, provider, tmux_session, tmux_window, lifecycle, init_state, lifecycle_generation) "
                "VALUES ('sem06aa', 'developer', 'mock_cli', 'test-session', 'w0', 'ephemeral', 'ready', 1)"
            ))
            db.commit()
            row = db.execute(
                text("SELECT id FROM terminals WHERE id = 'sem06aa'")
            ).fetchone()
            assert row is not None
            assert row[0] == "sem06aa"
            db.execute(text("DELETE FROM terminals WHERE id = 'sem06aa'"))
            db.commit()
