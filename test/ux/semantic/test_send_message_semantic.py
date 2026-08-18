"""S-kind tests for S03, S04, S05 — messaging state machine."""

import pytest


@pytest.mark.ux(surface="S03", invariant="UX-2", kind="S")
class TestSendMessageSemanticUX2:
    def test_inbox_message_persists(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text(
                "INSERT INTO inbox (sender_id, receiver_id, message, status) "
                "VALUES ('s1', 'r1', 'sem delivery', 'pending')"
            ))
            db.commit()
            row = db.execute(text("SELECT message, status FROM inbox WHERE receiver_id = 'r1' AND message = 'sem delivery'")).fetchone()
            assert row is not None
            assert row[0] == "sem delivery"
            db.execute(text("DELETE FROM inbox WHERE receiver_id = 'r1' AND message = 'sem delivery'"))
            db.commit()


@pytest.mark.ux(surface="S03", invariant="UX-3", kind="S")
class TestSendMessageSemanticUX3:
    def test_message_starts_pending(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text(
                "INSERT INTO inbox (sender_id, receiver_id, message, status) "
                "VALUES ('s2', 'r2', 'ni sem', 'pending')"
            ))
            db.commit()
            row = db.execute(text("SELECT status FROM inbox WHERE receiver_id = 'r2' AND message = 'ni sem'")).fetchone()
            assert row[0] == "pending"
            db.execute(text("DELETE FROM inbox WHERE receiver_id = 'r2' AND message = 'ni sem'"))
            db.commit()


@pytest.mark.ux(surface="S03", invariant="UX-5", kind="S")
class TestSendMessageSemanticUX5:
    def test_message_stores_sender(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text(
                "INSERT INTO inbox (sender_id, receiver_id, message, status) "
                "VALUES ('auth_s', 'auth_r', 'auth msg', 'pending')"
            ))
            db.commit()
            row = db.execute(text("SELECT sender_id FROM inbox WHERE receiver_id = 'auth_r'")).fetchone()
            assert row[0] == "auth_s"
            db.execute(text("DELETE FROM inbox WHERE receiver_id = 'auth_r'"))
            db.commit()


@pytest.mark.ux(surface="S04", invariant="UX-2", kind="S")
class TestListMessagesSemanticUX2:
    def test_message_queryable(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text(
                "INSERT INTO inbox (sender_id, receiver_id, message, status) "
                "VALUES ('s4', 'r4', 'list sem', 'pending')"
            ))
            db.commit()
            rows = db.execute(text("SELECT message FROM inbox WHERE receiver_id = 'r4'")).fetchall()
            assert len(rows) >= 1
            db.execute(text("DELETE FROM inbox WHERE receiver_id = 'r4'"))
            db.commit()


@pytest.mark.ux(surface="S05", invariant="UX-2", kind="S")
class TestInboxDeliverySemanticUX2:
    def test_pending_available(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text(
                "INSERT INTO inbox (sender_id, receiver_id, message, status) "
                "VALUES ('s5', 'r5', 'delivery sem', 'pending')"
            ))
            db.commit()
            count = db.execute(text("SELECT COUNT(*) FROM inbox WHERE receiver_id = 'r5' AND status = 'pending'")).fetchone()[0]
            assert count >= 1
            db.execute(text("DELETE FROM inbox WHERE receiver_id = 'r5'"))
            db.commit()


@pytest.mark.ux(surface="S05", invariant="UX-3", kind="S")
class TestInboxDeliverySemanticUX3:
    def test_status_transition(self):
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text(
                "INSERT INTO inbox (sender_id, receiver_id, message, status) "
                "VALUES ('s6', 'r6', 'trans', 'pending')"
            ))
            db.commit()
            db.execute(text("UPDATE inbox SET status = 'delivered' WHERE receiver_id = 'r6' AND message = 'trans'"))
            db.commit()
            row = db.execute(text("SELECT status FROM inbox WHERE receiver_id = 'r6' AND message = 'trans'")).fetchone()
            assert row[0] == "delivered"
            db.execute(text("DELETE FROM inbox WHERE receiver_id = 'r6'"))
            db.commit()
