"""resolve_supervisor_target(): the rung-1 registry probe reads the registry, nothing else.

Perf side lane 2026-09-02: ``has_registry`` used to come from
``resolve_target()`` — two tmux execs and a full /proc scan per supervisor
mailbox per 5 s convergence tick — although the only outcome consulted was
whether the registry had ANY record. Pin the cheaper predicate and prove the
expensive path is no longer touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
)
from cli_agent_orchestrator.services import cc_session_registry, delivery_service
from cli_agent_orchestrator.services.delivery_service import resolve_supervisor_target


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.SessionLocal", TestSession)
    monkeypatch.setattr(delivery_service, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(
            TerminalModel(
                id="sup1",
                tmux_session="cao-test",
                tmux_window="sup1",
                provider="claude_code",
                agent_profile="supervisor",
            )
        )
        s.add(
            MailboxModel(
                id="mb1",
                session_name="cao-test",
                role="supervisor",
                current_terminal_id="sup1",
                generation=1,
                consumed_through_id=0,
                cc_inbox_path="/nowhere/team-lead.json",
            )
        )
        s.add(MailboxIncarnationModel(mailbox_id="mb1", generation=1, terminal_id="sup1"))
        s.commit()
    return TestSession


def _forbid_expensive_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("expensive resolve path must not run for has_registry")

    monkeypatch.setattr(cc_session_registry, "resolve_target", boom)
    monkeypatch.setattr(cc_session_registry, "first_pane", boom)
    monkeypatch.setattr(cc_session_registry, "_resolve_tmux_window_id", boom)


def test_has_registry_true_when_registry_has_records(db, monkeypatch) -> None:
    _forbid_expensive_paths(monkeypatch)
    monkeypatch.setattr(cc_session_registry, "read_registry", lambda *a, **k: [MagicMock()])
    target = resolve_supervisor_target("mb1")
    assert target.terminal_id == "sup1"
    assert target.tmux_session == "cao-test"
    assert target.has_registry is True


def test_has_registry_false_when_registry_empty(db, monkeypatch) -> None:
    _forbid_expensive_paths(monkeypatch)
    monkeypatch.setattr(cc_session_registry, "read_registry", lambda *a, **k: [])
    assert resolve_supervisor_target("mb1").has_registry is False


def test_registry_read_failure_is_best_effort_false(db, monkeypatch) -> None:
    _forbid_expensive_paths(monkeypatch)

    def broken(*_a: object, **_k: object) -> list:
        raise OSError("sessions dir unreadable")

    monkeypatch.setattr(cc_session_registry, "read_registry", broken)
    target = resolve_supervisor_target("mb1")
    assert target.has_registry is False
    assert target.terminal_id == "sup1"


def test_registry_present_readmits_ejected_rung1(db, monkeypatch) -> None:
    """D11 self-heal still fires off the cheap predicate."""
    _forbid_expensive_paths(monkeypatch)
    monkeypatch.setattr(cc_session_registry, "read_registry", lambda *a, **k: [object()])
    from cli_agent_orchestrator.services import transport_ejection

    svc = transport_ejection.transport_ejection_service
    monkeypatch.setattr(svc, "is_ejected", lambda tid, rung: (tid, rung) == ("sup1", "rung1"))
    readmitted: list[tuple[str, str]] = []
    monkeypatch.setattr(svc, "readmit", lambda tid, rung: readmitted.append((tid, rung)))
    resolve_supervisor_target("mb1")
    assert readmitted == [("sup1", "rung1")]
