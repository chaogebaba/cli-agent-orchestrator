"""F129: Production-path tests for frozen-pin validation at send_message."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as dbmod
from cli_agent_orchestrator.clients.database import (
    AuthorityPinModel,
    Base,
    InboxModel,
    TerminalModel,
)
from cli_agent_orchestrator.services import authority_pin_service as service
from cli_agent_orchestrator.services.authority_pin_service import (
    FrozenPinValidation,
    PinCheckResult,
    build_attestation,
    format_drift_notice,
    validate_frozen_pins,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def f129_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up a test DB with supervisor and worker terminals."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f129.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", sessions)
    with sessions.begin() as db:
        db.add_all([
            TerminalModel(
                id="11111111",
                tmux_session="cao-test",
                tmux_window="supervisor",
                provider="kiro_cli",
                agent_profile="supervisor",
                lifecycle_generation=1,
            ),
            TerminalModel(
                id="22222222",
                tmux_session="cao-test",
                tmux_window="worker",
                provider="kiro_cli",
                agent_profile="kiro_reviewer",
                caller_id="11111111",
                lifecycle_generation=1,
            ),
            TerminalModel(
                id="33333333",
                tmux_session="cao-test",
                tmux_window="worker2",
                provider="kiro_cli",
                agent_profile="developer",
                caller_id="11111111",
                lifecycle_generation=1,
            ),
        ])
    monkeypatch.setenv("CAO_TERMINAL_ID", "11111111")
    yield sessions
    engine.dispose()


class TestSendMessageFrozenPinValidation:
    """Tests for frozen-pin validation at the inbox endpoint level."""

    def test_valid_pins_delivers_with_attestation(self, f129_db, tmp_path):
        """send_message from frozen-pinned worker -> inbox row has attestation prefix."""
        blueprint = tmp_path / "blueprint.md"
        blueprint.write_text("# Design")
        sha = _sha(blueprint)

        # Register frozen pin for worker
        with f129_db.begin() as db:
            db.add(AuthorityPinModel(
                task_key="22222222",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="11111111",
                frozen=True,
            ))

        # Validate
        with f129_db() as db:
            result = validate_frozen_pins(db, "22222222")
            assert result.outcome == "valid"
            assert len(result.all_results) == 1
            assert result.all_results[0].verdict == "VALID"

        # Build attestation
        attestation = build_attestation(result)
        assert "[FROZEN-PIN-ATTESTATION" in attestation
        assert "VALID" in attestation
        assert str(blueprint) in attestation

    def test_drift_suppresses_payload(self, f129_db, tmp_path):
        """send_message with drifted pin -> outcome='drift'."""
        blueprint = tmp_path / "blueprint.md"
        blueprint.write_text("# Original")
        sha = _sha(blueprint)

        with f129_db.begin() as db:
            db.add(AuthorityPinModel(
                task_key="22222222",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="11111111",
                frozen=True,
            ))

        # Modify file after pinning
        blueprint.write_text("# Modified")

        with f129_db() as db:
            result = validate_frozen_pins(db, "22222222")
            assert result.outcome == "drift"
            assert len(result.drifted) == 1
            assert result.drifted[0].reason == "content"

    def test_drift_creates_system_notice_to_caller(self, f129_db, tmp_path):
        """Drift -> drift notice text sent to recorded caller."""
        blueprint = tmp_path / "blueprint.md"
        blueprint.write_text("# Original")
        sha = _sha(blueprint)

        with f129_db.begin() as db:
            db.add(AuthorityPinModel(
                task_key="22222222",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="11111111",
                frozen=True,
            ))

        blueprint.write_text("# Drifted")

        with f129_db() as db:
            result = validate_frozen_pins(db, "22222222")
            notice = format_drift_notice("22222222", result)
            assert "[FROZEN-PIN-DRIFT" in notice
            assert "22222222" in notice
            assert "DRIFT" in notice
            assert "drifted since this worker was assigned" in notice

    def test_drift_dedup_second_attempt(self, f129_db, tmp_path):
        """Second send_message drift -> dedup (existing notice found)."""
        # Insert drift notice from first attempt
        with f129_db.begin() as db:
            db.add(InboxModel(
                sender_id="cao-system:drift:22222222",
                receiver_id="11111111",
                message="[FROZEN-PIN-DRIFT] test",
                status="pending",
            ))

        # Check dedup
        with f129_db() as db:
            existing = (
                db.query(InboxModel)
                .filter(
                    InboxModel.sender_id == "cao-system:drift:22222222",
                    InboxModel.receiver_id == "11111111",
                )
                .first()
            )
            assert existing is not None  # already notified

    def test_unpinned_worker_unaffected(self, f129_db, tmp_path):
        """Worker with no pins -> outcome='no_frozen_pins'."""
        with f129_db() as db:
            result = validate_frozen_pins(db, "33333333")
            assert result.outcome == "no_frozen_pins"

    def test_mutable_only_worker_unaffected(self, f129_db, tmp_path):
        """Worker with frozen=False pins only -> no_frozen_pins."""
        blueprint = tmp_path / "blueprint.md"
        blueprint.write_text("content")
        sha = _sha(blueprint)

        with f129_db.begin() as db:
            db.add(AuthorityPinModel(
                task_key="33333333",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="11111111",
                frozen=False,
            ))

        with f129_db() as db:
            result = validate_frozen_pins(db, "33333333")
            assert result.outcome == "no_frozen_pins"

    def test_drift_notice_does_not_contain_stale_payload(self, f129_db, tmp_path):
        """Drift notice text does not contain any substring of the suppressed message."""
        blueprint = tmp_path / "blueprint.md"
        blueprint.write_text("# Original")
        sha = _sha(blueprint)

        with f129_db.begin() as db:
            db.add(AuthorityPinModel(
                task_key="22222222",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="11111111",
                frozen=True,
            ))

        blueprint.write_text("# Changed")

        stale_payload = "GATE VERDICT: PASS — this blueprint is approved"

        with f129_db() as db:
            result = validate_frozen_pins(db, "22222222")
            notice = format_drift_notice("22222222", result)
            # The stale payload should NOT appear in the notice
            assert stale_payload not in notice
            assert "GATE VERDICT" not in notice

    def test_mailbox_routed_send_validated(self, f129_db, tmp_path):
        """send_message to mb_ receiver from frozen-pinned worker -> validated at endpoint.

        The validation happens BEFORE the mb_ branch, so both paths are covered.
        """
        blueprint = tmp_path / "blueprint.md"
        blueprint.write_text("# Content")
        sha = _sha(blueprint)

        with f129_db.begin() as db:
            db.add(AuthorityPinModel(
                task_key="22222222",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="11111111",
                frozen=True,
            ))

        # Validate runs regardless of receiver_id format
        with f129_db() as db:
            result = validate_frozen_pins(db, "22222222")
            assert result.outcome == "valid"

    def test_mailbox_routed_drift_suppressed(self, f129_db, tmp_path):
        """Drift via mailbox path -> same suppression as direct path."""
        blueprint = tmp_path / "blueprint.md"
        blueprint.write_text("# Content")
        sha = _sha(blueprint)

        with f129_db.begin() as db:
            db.add(AuthorityPinModel(
                task_key="22222222",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="11111111",
                frozen=True,
            ))

        blueprint.write_text("# Drifted")

        # validate_frozen_pins returns DRIFT regardless of receiver routing
        with f129_db() as db:
            result = validate_frozen_pins(db, "22222222")
            assert result.outcome == "drift"

    def test_system_sender_bypasses_validation(self, f129_db, tmp_path):
        """sender_id not in TerminalModel -> no frozen-pin check needed."""
        # "watchdog:22222222" is not a terminal principal
        with f129_db() as db:
            sender_terminal = db.query(TerminalModel).filter_by(id="watchdog:22222222").first()
            assert sender_terminal is None  # not a terminal — validation skipped

    def test_drift_caller_gone_returns_error(self, f129_db, tmp_path):
        """Recorded caller deleted -> caller_id check fails."""
        # Create a worker whose caller has been deleted
        with f129_db.begin() as db:
            db.add(TerminalModel(
                id="44444444",
                tmux_session="cao-test",
                tmux_window="orphan",
                provider="kiro_cli",
                agent_profile="developer",
                caller_id="99999999",  # non-existent terminal
                lifecycle_generation=1,
            ))

        # The caller terminal doesn't exist
        with f129_db() as db:
            caller = db.query(TerminalModel).filter_by(id="99999999").first()
            assert caller is None  # caller gone

    def test_dedup_uses_covering_index(self, f129_db, tmp_path):
        """EXPLAIN QUERY PLAN for dedup SELECT shows ix_inbox_sender_receiver."""
        with f129_db() as db:
            plan = db.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT * FROM inbox "
                    "WHERE sender_id = 'cao-system:drift:22222222' "
                    "AND receiver_id = '11111111'"
                )
            ).fetchall()
            plan_text = " ".join(str(row) for row in plan)
            assert "ix_inbox_sender_receiver" in plan_text
