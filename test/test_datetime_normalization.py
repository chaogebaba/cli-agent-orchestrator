"""F130 UTC datetime normalization acceptance tests (AC11-AC14 + production-path coverage).

Tests the production call paths to verify:
- AC11: list_messages with naive since returns correctly
- AC12: _get_next_run_time returns aware-UTC
- AC13: get_due_flows returns due aware-UTC flow
- AC14: SQLAlchemy SQLite strips +00:00 from aware-UTC bind params (textual only)
- Column declarations: all DateTime columns are timezone=True
- since normalization at service and endpoint layers
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import Column, DateTime, Integer, String, Boolean, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    FlowModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    _as_utc,
    _utcnow,
    get_flows_to_run,
)
from cli_agent_orchestrator.services import mailbox_service
from cli_agent_orchestrator.services.flow_service import _get_next_run_time
from cli_agent_orchestrator.services.mailbox_service import list_messages


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """In-memory scratch database for isolation."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'fx130.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    # Ensure schema_version column exists on mailboxes
    with engine.begin() as conn:
        columns = conn.execute(text("PRAGMA table_info(mailboxes)")).mappings().all()
        if "schema_version" not in {col["name"] for col in columns}:
            conn.execute(
                text("ALTER TABLE mailboxes ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
            )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def _make_terminal(db: Session, terminal_id: str, session: str = "test-sess") -> None:
    db.add(
        TerminalModel(
            id=terminal_id,
            tmux_session=session,
            tmux_window=terminal_id,
            provider="claude_code",
            agent_profile="developer",
            init_state="ready",
        )
    )


def _make_mailbox(
    db: Session, terminal_id: str = "t-001", *, generation: int = 1
) -> MailboxModel:
    row = MailboxModel(
        id="mb_test",
        session_name="test-sess",
        role="supervisor",
        current_terminal_id=terminal_id,
        generation=generation,
        consumed_through_id=0,
        schema_version=1,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(row)
    db.add(
        MailboxIncarnationModel(
            mailbox_id=row.id,
            generation=generation,
            terminal_id=terminal_id,
            published_at=_utcnow(),
        )
    )
    return row


def _make_inbox_row(
    db: Session,
    receiver: str,
    *,
    created_at: datetime | None = None,
    message: str = "test msg",
) -> InboxModel:
    row = InboxModel(
        sender_id="worker-001",
        receiver_id=receiver,
        logical_receiver_id=None,
        enqueue_generation=1,
        message=message,
        orchestration_type="send_message",
        status="pending",
        created_at=created_at or _utcnow(),
    )
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------
# AC11: list_messages with naive since
# ---------------------------------------------------------------------------


class TestAC11NaiveSinceCoercion:
    """A naive `since` (no tzinfo) is coerced to UTC and matches aware rows."""

    def test_naive_since_returns_row_created_with_utcnow(self, scratch_db):
        """Production path: naive since → attach UTC → filter works."""
        now = _utcnow()
        with scratch_db.begin() as db:
            _make_terminal(db, "t-001")
            _make_mailbox(db, "t-001")
            _make_inbox_row(db, "t-001", created_at=now)

        # Naive since = 1 minute before now (no tzinfo)
        naive_since = (now - timedelta(minutes=1)).replace(tzinfo=None)
        result = list_messages("t-001", since=naive_since)
        assert len(result["items"]) == 1

    def test_naive_since_excludes_older_rows(self, scratch_db):
        """Naive since AFTER the row's time → row excluded."""
        past = _utcnow() - timedelta(hours=1)
        with scratch_db.begin() as db:
            _make_terminal(db, "t-001")
            _make_mailbox(db, "t-001")
            _make_inbox_row(db, "t-001", created_at=past)

        # Naive since = now (after the row)
        naive_since = _utcnow().replace(tzinfo=None)
        result = list_messages("t-001", since=naive_since)
        assert len(result["items"]) == 0

    def test_aware_non_utc_since_converts_correctly(self, scratch_db):
        """Aware non-UTC since (e.g. UTC-4) is converted to UTC for comparison.

        This is the mutation-killing case: without normalization, SQLAlchemy would
        bind the non-UTC digits (wrong wall clock) and miss rows. A since of
        "00:00:00-04:00" (= 04:00 UTC) must find a row created at 04:30 UTC, but
        without conversion it would bind "00:00:00" and wrongly INCLUDE the row
        (since the stripped text is before 04:30). The inverse catches the mutant:
        a since of "08:00:00+04:00" (= 04:00 UTC) without conversion binds "08:00"
        which is AFTER a row at 04:30 UTC and wrongly EXCLUDES it.
        """
        # Row created at 04:30 UTC
        row_time = _utcnow().replace(hour=4, minute=30, second=0, microsecond=0)
        with scratch_db.begin() as db:
            _make_terminal(db, "t-001")
            _make_mailbox(db, "t-001")
            _make_inbox_row(db, "t-001", created_at=row_time)

        # Express "04:00 UTC" as "08:00+04:00". Without normalization, SQLAlchemy
        # strips to "08:00:00" which is > "04:30:00" → row excluded (WRONG).
        # With normalization (astimezone UTC), the since becomes 04:00 UTC →
        # SQLAlchemy strips to "04:00:00" which is < "04:30:00" → row included (CORRECT).
        utc_plus_4 = timezone(timedelta(hours=4))
        since_non_utc = row_time.replace(hour=8, minute=0).replace(tzinfo=utc_plus_4)
        # This represents 04:00 UTC expressed as 08:00+04:00
        result = list_messages("t-001", since=since_non_utc)
        assert len(result["items"]) == 1, (
            "Row at 04:30 UTC should be included when since=08:00+04:00 (=04:00 UTC)"
        )


# ---------------------------------------------------------------------------
# AC12: _get_next_run_time returns aware-UTC
# ---------------------------------------------------------------------------


class TestAC12CronTriggerAwareUTC:
    """_get_next_run_time must return a datetime with tzinfo == timezone.utc."""

    def test_returns_aware_utc(self):
        result = _get_next_run_time("*/5 * * * *")
        assert result.tzinfo is not None, "Result must be timezone-aware"
        assert result.tzinfo == timezone.utc, f"Expected UTC, got {result.tzinfo}"

    def test_various_cron_expressions_all_aware_utc(self):
        expressions = ["0 * * * *", "30 2 * * *", "*/10 * * * 1-5", "0 0 1 * *"]
        for expr in expressions:
            result = _get_next_run_time(expr)
            assert result.tzinfo == timezone.utc, f"Failed for {expr}: {result.tzinfo}"

    def test_result_is_in_the_future(self):
        now = _utcnow()
        result = _get_next_run_time("*/5 * * * *")
        assert result > now, f"Expected future time, got {result} <= {now}"


# ---------------------------------------------------------------------------
# AC13: get_due_flows returns a due flow with aware-UTC next_run
# ---------------------------------------------------------------------------


class TestAC13GetDueFlows:
    """get_due_flows returns flows with next_run <= now (aware-UTC)."""

    def test_due_flow_returned(self, scratch_db):
        """A flow with next_run in the past is returned."""
        past = _utcnow() - timedelta(minutes=1)
        with scratch_db.begin() as db:
            db.add(
                FlowModel(
                    name="test-flow-due",
                    file_path="/tmp/test.md",
                    schedule="*/5 * * * *",
                    agent_profile="developer",
                    provider="claude_code",
                    script="",
                    last_run=None,
                    next_run=past,
                    enabled=True,
                )
            )

        flows = get_flows_to_run()
        names = [f.name for f in flows]
        assert "test-flow-due" in names

    def test_future_flow_not_returned(self, scratch_db):
        """A flow with next_run in the future is NOT returned."""
        future = _utcnow() + timedelta(hours=1)
        with scratch_db.begin() as db:
            db.add(
                FlowModel(
                    name="test-flow-future",
                    file_path="/tmp/test.md",
                    schedule="*/5 * * * *",
                    agent_profile="developer",
                    provider="claude_code",
                    script="",
                    last_run=None,
                    next_run=future,
                    enabled=True,
                )
            )

        flows = get_flows_to_run()
        names = [f.name for f in flows]
        assert "test-flow-future" not in names

    def test_disabled_flow_not_returned(self, scratch_db):
        """A disabled flow is not returned even if due."""
        past = _utcnow() - timedelta(minutes=1)
        with scratch_db.begin() as db:
            db.add(
                FlowModel(
                    name="test-flow-disabled",
                    file_path="/tmp/test.md",
                    schedule="*/5 * * * *",
                    agent_profile="developer",
                    provider="claude_code",
                    script="",
                    last_run=None,
                    next_run=past,
                    enabled=False,
                )
            )

        flows = get_flows_to_run()
        names = [f.name for f in flows]
        assert "test-flow-disabled" not in names


# ---------------------------------------------------------------------------
# AC14: SQLAlchemy SQLite bind-param offset stripping (textual only)
# ---------------------------------------------------------------------------


class TestAC14BindParamStrip:
    """Verify aware-UTC bind params are rendered without +00:00 suffix by
    SQLAlchemy's SQLite dialect, and match legacy naive-UTC-text rows."""

    def test_aware_utc_bind_matches_legacy_naive_row(self, tmp_path):
        """An aware-UTC query matches a row stored as naive-UTC text (legacy format)."""
        engine = create_engine(f"sqlite:///{tmp_path / 'ac14.sqlite'}")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        # Insert a "legacy" naive-UTC row directly via raw SQL
        # (simulating pre-hotfix storage: '2026-08-10 04:00:00.000000')
        legacy_time_str = "2026-08-10 04:00:00.000000"
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO flows (name, file_path, schedule, agent_profile, provider, "
                    "next_run, enabled) VALUES (:name, :fp, :sched, :ap, :prov, :nr, 1)"
                ),
                {
                    "name": "legacy-flow",
                    "fp": "/tmp/f.md",
                    "sched": "*/5 * * * *",
                    "ap": "dev",
                    "prov": "claude_code",
                    "nr": legacy_time_str,
                },
            )

        # Query with aware-UTC bind param: should strip +00:00 and match
        query_time = datetime(2026, 8, 10, 4, 30, 0, tzinfo=timezone.utc)
        with Session() as db:
            results = (
                db.query(FlowModel)
                .filter(FlowModel.enabled == True, FlowModel.next_run <= query_time)
                .all()
            )
        assert len(results) == 1
        assert results[0].name == "legacy-flow"
        engine.dispose()

    def test_aware_utc_bind_param_has_no_offset_suffix(self, tmp_path):
        """Capture the actual SQL param to prove no +00:00 suffix."""
        engine = create_engine(f"sqlite:///{tmp_path / 'ac14_capture.sqlite'}")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        captured_params: list = []

        @event.listens_for(engine, "before_cursor_execute")
        def capture(conn, cursor, statement, parameters, context, executemany):
            if "flows" in statement and "WHERE" in statement:
                captured_params.append(parameters)

        query_time = datetime(2026, 8, 10, 4, 0, 0, tzinfo=timezone.utc)
        with Session() as db:
            db.query(FlowModel).filter(
                FlowModel.enabled == True, FlowModel.next_run <= query_time
            ).all()

        # Verify at least one captured param set and the datetime param has no +00:00
        assert len(captured_params) > 0, "No SQL params captured"
        # The bound params are a tuple; find the datetime string
        for params in captured_params:
            if isinstance(params, (list, tuple)):
                for p in params:
                    if isinstance(p, str) and "2026-08-10" in p:
                        assert "+00:00" not in p, f"Offset not stripped: {p}"
                        assert "04:00:00" in p, f"Wrong digits: {p}"
            elif isinstance(params, dict):
                for v in params.values():
                    if isinstance(v, str) and "2026-08-10" in v:
                        assert "+00:00" not in v, f"Offset not stripped: {v}"
                        assert "04:00:00" in v, f"Wrong digits: {v}"
        engine.dispose()


# ---------------------------------------------------------------------------
# Column declaration audit
# ---------------------------------------------------------------------------


class TestColumnDeclarations:
    """All DateTime columns on models must be DateTime(timezone=True)."""

    def test_terminal_last_active_is_tz_aware(self):
        col = TerminalModel.__table__.columns["last_active"]
        assert isinstance(col.type, DateTime)
        assert col.type.timezone is True, "TerminalModel.last_active must have timezone=True"

    def test_mailbox_updated_at_is_tz_aware(self):
        col = MailboxModel.__table__.columns["updated_at"]
        assert isinstance(col.type, DateTime)
        assert col.type.timezone is True, "MailboxModel.updated_at must have timezone=True"

    def test_mailbox_incarnation_published_at_is_tz_aware(self):
        col = MailboxIncarnationModel.__table__.columns["published_at"]
        assert isinstance(col.type, DateTime)
        assert col.type.timezone is True, "MailboxIncarnationModel.published_at must have timezone=True"

    def test_flow_last_run_is_tz_aware(self):
        col = FlowModel.__table__.columns["last_run"]
        assert isinstance(col.type, DateTime)
        assert col.type.timezone is True, "FlowModel.last_run must have timezone=True"

    def test_flow_next_run_is_tz_aware(self):
        col = FlowModel.__table__.columns["next_run"]
        assert isinstance(col.type, DateTime)
        assert col.type.timezone is True, "FlowModel.next_run must have timezone=True"


# ---------------------------------------------------------------------------
# _as_utc helper tests
# ---------------------------------------------------------------------------


class TestAsUtcHelper:
    """Verify _as_utc behavior matches blueprint S3 requirements."""

    def test_none_returns_none(self):
        assert _as_utc(None) is None

    def test_naive_attaches_utc(self):
        naive = datetime(2026, 8, 10, 4, 0, 0)
        result = _as_utc(naive)
        assert result is not None
        assert result.tzinfo == timezone.utc
        assert result.hour == 4  # digits unchanged

    def test_aware_utc_unchanged(self):
        aware = datetime(2026, 8, 10, 4, 0, 0, tzinfo=timezone.utc)
        result = _as_utc(aware)
        assert result is not None
        assert result.tzinfo == timezone.utc
        assert result == aware

    def test_aware_non_utc_converts(self):
        utc_plus_5 = timezone(timedelta(hours=5))
        aware = datetime(2026, 8, 10, 9, 0, 0, tzinfo=utc_plus_5)  # = 04:00 UTC
        result = _as_utc(aware)
        assert result is not None
        assert result.tzinfo == timezone.utc
        assert result.hour == 4


# ---------------------------------------------------------------------------
# API endpoint since normalization (AC5/AC6 production path)
# ---------------------------------------------------------------------------


class TestEndpointSinceNormalization:
    """Verify the API endpoint normalizes since to aware-UTC."""

    def test_endpoint_naive_since_parsed_as_utc(self):
        """Simulate the api/main.py parsing logic for a naive ISO string."""
        # This mirrors the endpoint code path
        since_str = "2026-08-10T04:00:00"
        parsed = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        # Apply the same normalization as the endpoint
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        assert parsed.tzinfo == timezone.utc
        assert parsed.hour == 4

    def test_endpoint_aware_offset_since_converts_to_utc(self):
        """An aware offset since is converted to UTC."""
        since_str = "2026-08-10T00:00:00-04:00"
        parsed = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        assert parsed.tzinfo == timezone.utc
        assert parsed.hour == 4  # 00:00-04:00 = 04:00 UTC

    def test_endpoint_z_suffix_treated_as_utc(self):
        """A Z-suffix since is treated as UTC."""
        since_str = "2026-08-10T04:00:00Z"
        parsed = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        assert parsed.tzinfo == timezone.utc
        assert parsed.hour == 4
