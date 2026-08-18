"""S-kind tests for S02 (handoff) — UX-1, UX-4."""

import pytest

_COLS = "id, agent_profile, provider, tmux_session, tmux_window, lifecycle, init_state, lifecycle_generation"


@pytest.mark.ux(surface="S02", invariant="UX-1", kind="S")
class TestHandoffSemanticUX1:
    def test_terminal_stores_caller_id(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text(
                f"INSERT INTO terminals ({_COLS}, caller_id) "
                "VALUES ('sem02aa', 'developer', 'mock_cli', 'ts', 'w0', 'ephemeral', 'ready', 1, 'sup11111')"
            ))
            db.commit()
            row = db.execute(text("SELECT caller_id FROM terminals WHERE id = 'sem02aa'")).fetchone()
            assert row[0] == "sup11111"
            db.execute(text("DELETE FROM terminals WHERE id = 'sem02aa'"))
            db.commit()


@pytest.mark.ux(surface="S02", invariant="UX-4", kind="S")
class TestHandoffSemanticUX4:
    def test_caller_id_enables_routing(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text(
                f"INSERT INTO terminals ({_COLS}) "
                "VALUES ('sem04su', 'developer', 'mock_cli', 'ts', 'w0', 'ephemeral', 'ready', 1)"
            ))
            db.execute(text(
                f"INSERT INTO terminals ({_COLS}, caller_id) "
                "VALUES ('sem04wk', 'developer', 'mock_cli', 'ts', 'w1', 'ephemeral', 'ready', 1, 'sem04su')"
            ))
            db.commit()
            row = db.execute(text("SELECT caller_id FROM terminals WHERE id = 'sem04wk'")).fetchone()
            assert row[0] == "sem04su"
            db.execute(text("DELETE FROM terminals WHERE id IN ('sem04su', 'sem04wk')"))
            db.commit()
