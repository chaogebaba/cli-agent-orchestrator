"""Shared wiring for the F875 desk-service acceptance tests.

Every test drives the real ``desk_service`` / ``desk_reconciler`` logic against
a fresh tmp sqlite engine and a deterministic ``SimClock`` — no live provider,
tmux, or HTTP. The fixture repoints the module-level ``SessionLocal`` / ``engine``
seams the service and reconciler bind at import time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import cli_agent_orchestrator.clients.database as db
import cli_agent_orchestrator.services.desk_reconciler as dr
import cli_agent_orchestrator.services.desk_service as ds
from cli_agent_orchestrator.sim.clock import SimClock, install


class DeskRig:
    """A wired desk environment: engine + a clock the test advances."""

    def __init__(self, engine, SessionLocal, clock: SimClock) -> None:
        self.engine = engine
        self.SessionLocal = SessionLocal
        self.clock = clock

    def seed_live_conversation(
        self, identity_key: str, *, provider: str = "pi_cli", generation: int = 0
    ) -> None:
        now = self.clock.utcnow().isoformat(sep=" ")
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversation_identity "
                    "(identity_key, provider, generation, lifecycle, origin, "
                    " created_at, updated_at) "
                    "VALUES (:k, :p, :g, 'live', 'spawn', :now, :now)"
                ),
                {"k": identity_key, "p": provider, "g": generation, "now": now},
            )

    def set_lifecycle(self, identity_key: str, lifecycle: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE conversation_identity SET lifecycle = :lc " "WHERE identity_key = :k"),
                {"lc": lifecycle, "k": identity_key},
            )


@pytest.fixture
def desk_rig(tmp_path, monkeypatch):
    """A fresh desk environment with an installed SimClock for the test body."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'desk.db'}", connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", SessionLocal)
    monkeypatch.setattr(ds, "SessionLocal", SessionLocal)
    monkeypatch.setattr(dr, "SessionLocal", SessionLocal)
    monkeypatch.setattr(dr, "engine", engine)

    clock = SimClock(initial_wall=datetime(2026, 1, 1, tzinfo=timezone.utc))
    with install(clock):
        yield DeskRig(engine, SessionLocal, clock)


def ready_boundary(cid: str, inc: str):
    from cli_agent_orchestrator.services.desk_reconciler import CreateOutcome, CreateStatus

    return CreateOutcome(
        CreateStatus.READY, provider_binding="pi_cli:secretary@cell", terminal_ref="t-desk"
    )


def degraded_boundary(status_name: str, retry_after: Optional[int] = None) -> Callable:
    from cli_agent_orchestrator.services.desk_reconciler import CreateOutcome, CreateStatus

    status = CreateStatus(status_name)

    def _b(cid: str, inc: str) -> CreateOutcome:
        return CreateOutcome(status, retry_after_seconds=retry_after)

    return _b
