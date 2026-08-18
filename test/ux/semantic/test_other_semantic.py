"""S-kind tests for S06, S07, S08, S09, S10, S11."""

import hashlib

import pytest


@pytest.mark.ux(surface="S06", invariant="UX-5", kind="S")
class TestAuthorityPinsSemanticUX5:
    def test_hash_file_computes_sha256(self, tmp_path):
        f = tmp_path / "pin.py"
        f.write_text("content")
        expected = hashlib.sha256(f.read_bytes()).hexdigest()
        from cli_agent_orchestrator.services.authority_pin_service import _hash_file
        result, err = _hash_file(str(f))
        assert err is None
        assert result == expected

    def test_hash_detects_drift(self, tmp_path):
        f = tmp_path / "drift.py"
        f.write_text("original")
        orig = hashlib.sha256(f.read_bytes()).hexdigest()
        f.write_text("mutated")
        from cli_agent_orchestrator.services.authority_pin_service import _hash_file
        new, err = _hash_file(str(f))
        assert err is None
        assert new != orig


@pytest.mark.ux(surface="S07", invariant="UX-4", kind="S")
class TestBarrierSemanticUX4:
    def test_barrier_table_exists(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            tables = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%barrier%'")).fetchall()
            assert len(tables) >= 1


@pytest.mark.ux(surface="S08", invariant="UX-4", kind="S")
class TestWorkflowSemanticUX4:
    def test_workflow_tables_exist(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            tables = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%workflow%'")).fetchall()
            assert any("workflow" in t[0] for t in tables)


@pytest.mark.ux(surface="S09", invariant="UX-3", kind="S")
class TestAutoResponderSemanticUX3:
    def test_auto_responder_exists(self):
        from cli_agent_orchestrator.services.auto_responder import AutoResponder
        assert AutoResponder is not None


@pytest.mark.ux(surface="S10", invariant="UX-6", kind="S")
class TestFleetSemanticUX6:
    def test_terminal_queryable_by_session(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text(
                "INSERT OR IGNORE INTO terminals (id, agent_profile, provider, tmux_session, tmux_window, lifecycle, init_state, lifecycle_generation) "
                "VALUES ('semflt1', 'developer', 'mock_cli', 'sem-fleet-s', 'w0', 'ephemeral', 'ready', 1)"
            ))
            db.commit()
            rows = db.execute(text("SELECT id FROM terminals WHERE tmux_session = 'sem-fleet-s'")).fetchall()
            assert any(r[0] == "semflt1" for r in rows)
            db.execute(text("DELETE FROM terminals WHERE id = 'semflt1'"))
            db.commit()


@pytest.mark.ux(surface="S11", invariant="UX-6", kind="S")
class TestSiblingsSemanticUX6:
    def test_terminal_record_queryable(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text(
                "INSERT OR IGNORE INTO terminals (id, agent_profile, provider, tmux_session, tmux_window, lifecycle, init_state, lifecycle_generation) "
                "VALUES ('semsib1', 'developer', 'mock_cli', 'test-session', 'w0', 'ephemeral', 'ready', 1)"
            ))
            db.commit()
            row = db.execute(text("SELECT id FROM terminals WHERE id = 'semsib1'")).fetchone()
            assert row is not None
            db.execute(text("DELETE FROM terminals WHERE id = 'semsib1'"))
            db.commit()
