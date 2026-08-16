"""F129: Production-entry integration tests — kill M1/M3/M4/M5/M6/M7/M9/M14/M15.

These tests drive REAL production functions (not helpers under test):
  1. database.create_terminal / create_terminal_with_warm_intent with authority_files
     inside an actual DB transaction → frozen pin publication + rollback (M1, M3)
  2. FastAPI TestClient against create_inbox_message_endpoint with actual DB rows →
     attestation, drift suppression, caller routing, dedup, terminal-sender
     classification / system bypass, endpoint placement (M4, M5, M6, M7, M14, M15)
  3. terminal_service.create_terminal with infrastructure/provider/tmux mocks →
     deferred initial_message gains [FROZEN-AUTHORITY-PINS] block (M9)

Also covers N1: logger.debug(..., exc_info=True) assertion for best-effort exception path.

Mutant partition (complete M1–M22, every ID exactly once):
  Killed HERE (production-entry):  M1, M3, M4, M5, M6, M7, M9, M14, M15
  Killed in test_authority_pin_service.py: M2, M8, M10, M11, M12, M13, M16
  Killed in test_f129_migration.py: M17, M18, M19, M20, M21, M22
  Total: 9 + 7 + 6 = 22  ✓
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.clients import database as dbmod
from cli_agent_orchestrator.clients.database import (
    AuthorityPinModel,
    Base,
    InboxModel,
    TerminalModel,
    create_terminal as db_create_terminal,
    create_terminal_with_warm_intent,
)
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services.authority_pin_service import AuthorityPinError


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def prod_db(tmp_path, monkeypatch):
    """Real SQLite database with full schema for production-entry tests."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f129_prod.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", sessions)
    # Seed a supervisor terminal
    with sessions.begin() as db:
        db.add(TerminalModel(
            id="aaaaaaaa",
            tmux_session="cao-test",
            tmux_window="supervisor",
            provider="kiro_cli",
            agent_profile="supervisor",
            lifecycle_generation=1,
        ))
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    yield sessions
    engine.dispose()


@pytest.fixture
def api_client(monkeypatch):
    """FastAPI TestClient with required mocks for inbox endpoint."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_guard_service."
        "get_ready_provider_session_by_source_terminal",
        lambda _terminal_id: None,
    )
    app.state.plugin_registry = PluginRegistry()
    return TestClient(app, headers={"Host": "localhost"})


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 1: database.create_terminal / create_terminal_with_warm_intent
#           — kills M1 and M3
# ═════════════════════════════════════════════════════════════════════════════


class TestDatabaseCreateTerminalFrozenPins:
    """Production database.create_terminal with authority_files in real transaction."""

    def test_create_terminal_publishes_frozen_pins_in_same_transaction(
        self, prod_db, tmp_path
    ):
        """M1: register_frozen_pins IS called inside create_terminal transaction.

        Mutant M1 removes the call to register_frozen_pins from create_terminal.
        This test calls the REAL database.create_terminal and asserts frozen pin
        rows exist in the database afterward.
        """
        blueprint = tmp_path / "blueprint.md"
        blueprint.write_text("# Production design doc")
        sha = _sha(blueprint)

        result = db_create_terminal(
            terminal_id="bbbbbbbb",
            tmux_session="cao-test",
            tmux_window="worker1",
            provider="kiro_cli",
            agent_profile="kiro_reviewer",
            caller_id="aaaaaaaa",
            authority_files=[{"file_path": str(blueprint), "sha256": sha}],
        )

        assert result["id"] == "bbbbbbbb"

        # Verify frozen pins were published in the same transaction
        with prod_db() as db:
            pins = db.query(AuthorityPinModel).filter_by(task_key="bbbbbbbb").all()
            assert len(pins) == 1
            assert pins[0].frozen is True
            assert pins[0].sha256 == sha
            assert pins[0].file_path == str(blueprint)
            assert pins[0].version == 1
            assert pins[0].registered_by == "aaaaaaaa"

    def test_create_terminal_hash_mismatch_rolls_back_both_terminal_and_pins(
        self, prod_db, tmp_path
    ):
        """M3: hash mismatch in register_frozen_pins raises and rolls back transaction.

        Mutant M3 wraps register_frozen_pins in a try/except that swallows errors.
        This test proves that a hash mismatch prevents both terminal row and pin
        rows from being committed.
        """
        blueprint = tmp_path / "blueprint.md"
        blueprint.write_text("# Real content")
        wrong_sha = hashlib.sha256(b"wrong content").hexdigest()

        with pytest.raises(AuthorityPinError, match="authority_hash_mismatch"):
            db_create_terminal(
                terminal_id="cccccccc",
                tmux_session="cao-test",
                tmux_window="worker2",
                provider="kiro_cli",
                agent_profile="kiro_reviewer",
                caller_id="aaaaaaaa",
                authority_files=[{"file_path": str(blueprint), "sha256": wrong_sha}],
            )

        # Neither terminal nor pin rows should exist
        with prod_db() as db:
            terminal = db.query(TerminalModel).filter_by(id="cccccccc").first()
            assert terminal is None, "Terminal row must be rolled back on pin failure"
            pins = db.query(AuthorityPinModel).filter_by(task_key="cccccccc").all()
            assert pins == [], "Pin rows must be rolled back on pin failure"

    def test_create_terminal_with_warm_intent_publishes_frozen_pins(
        self, prod_db, tmp_path
    ):
        """M1 variant: frozen pins also work through the warm-intent path."""
        blueprint = tmp_path / "design.md"
        blueprint.write_text("# Warm intent design")
        sha = _sha(blueprint)

        result = create_terminal_with_warm_intent(
            terminal_id="dddddddd",
            tmux_session="cao-test",
            tmux_window="warm-worker",
            provider="kiro_cli",
            agent_profile="kiro_reviewer",
            allowed_tools=None,
            caller_id="aaaaaaaa",
            parent_base_name=None,
            fork_mode=None,
            authority_files=[{"file_path": str(blueprint), "sha256": sha}],
        )

        assert result["id"] == "dddddddd"

        with prod_db() as db:
            pins = db.query(AuthorityPinModel).filter_by(task_key="dddddddd").all()
            assert len(pins) == 1
            assert pins[0].frozen is True
            assert pins[0].sha256 == sha


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 2: TestClient against create_inbox_message_endpoint
#           — kills M4, M5, M6, M7, M14, M15
# ═════════════════════════════════════════════════════════════════════════════


class TestEndpointFrozenPinValidation:
    """Real HTTP endpoint tests for frozen-pin validation via TestClient."""

    def test_valid_attestation_prepended_to_message(
        self, prod_db, tmp_path, api_client, monkeypatch
    ):
        """M4: validate_frozen_pins IS called in the endpoint, and attestation is prepended.

        Mutant M4 removes the entire validation block from the endpoint.
        This test posts to the real endpoint and verifies the stored inbox row
        contains the [FROZEN-PIN-ATTESTATION] block prepended to the message.
        """
        blueprint = tmp_path / "bp.md"
        blueprint.write_text("# Blueprint for attestation")
        sha = _sha(blueprint)

        # Set up worker with frozen pin
        with prod_db.begin() as db:
            db.add(TerminalModel(
                id="11111111",
                tmux_session="cao-test",
                tmux_window="attesting-worker",
                provider="kiro_cli",
                agent_profile="kiro_reviewer",
                caller_id="aaaaaaaa",
                lifecycle_generation=1,
            ))
            db.add(AuthorityPinModel(
                task_key="11111111",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="aaaaaaaa",
                frozen=True,
            ))

        with (
            patch(
                "cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed"
            ),
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
            patch("cli_agent_orchestrator.api.main.get_backend") as mock_backend,
        ):
            mock_backend.return_value.session_exists.return_value = True
            mock_backend.return_value.get_history.return_value = ""
            response = api_client.post(
                "/terminals/aaaaaaaa/inbox/messages",
                params={"sender_id": "11111111", "message": "GATE-PASS result"},
            )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["success"] is True

        # Verify the stored message has attestation prepended
        with prod_db() as db:
            row = db.query(InboxModel).filter_by(id=data["message_id"]).first()
            assert row is not None
            assert "[FROZEN-PIN-ATTESTATION" in row.message
            assert "GATE-PASS result" in row.message
            assert row.message.startswith("[FROZEN-PIN-ATTESTATION")

    def test_drift_suppresses_worker_payload(
        self, prod_db, tmp_path, api_client, monkeypatch
    ):
        """M5: drift outcome returns error and suppresses payload.

        Mutant M5 changes the drift outcome condition to never-match, so drift
        messages pass through unchanged. This test modifies the file after pinning
        and verifies the endpoint returns an error and does NOT store the payload.
        """
        blueprint = tmp_path / "bp_drift.md"
        blueprint.write_text("# Original")
        sha = _sha(blueprint)

        with prod_db.begin() as db:
            db.add(TerminalModel(
                id="22222222",
                tmux_session="cao-test",
                tmux_window="drifting-worker",
                provider="kiro_cli",
                agent_profile="kiro_reviewer",
                caller_id="aaaaaaaa",
                lifecycle_generation=1,
            ))
            db.add(AuthorityPinModel(
                task_key="22222222",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="aaaaaaaa",
                frozen=True,
            ))

        # Mutate the file AFTER pinning
        blueprint.write_text("# TAMPERED")

        with (
            patch(
                "cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed"
            ),
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
        ):
            response = api_client.post(
                "/terminals/aaaaaaaa/inbox/messages",
                params={
                    "sender_id": "22222222",
                    "message": "STALE VERDICT — this should be suppressed",
                },
            )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["success"] is False
        assert data["error"]["code"] == "frozen_pin_drift"
        assert len(data["error"]["drifted"]) == 1
        assert data["error"]["drifted"][0]["reason"] == "content"

        # Verify: no inbox row with the worker as sender was stored (payload suppressed)
        with prod_db() as db:
            worker_sent_rows = db.query(InboxModel).filter(
                InboxModel.sender_id == "22222222"
            ).all()
            assert worker_sent_rows == [], "Drifted worker payload must be suppressed"
            # The only inbox row is the drift notice (system sender → caller)
            drift_notice_rows = db.query(InboxModel).filter(
                InboxModel.sender_id == "cao-system:drift:22222222"
            ).all()
            assert len(drift_notice_rows) == 1
            assert "STALE VERDICT" not in drift_notice_rows[0].message

    def test_drift_notice_routed_to_caller_not_receiver(
        self, prod_db, tmp_path, api_client, monkeypatch
    ):
        """M6: drift notice goes to recorded caller, NOT the message receiver.

        Mutant M6 sends the notice to receiver_id instead of caller_id.
        This test sends to a third-party receiver and confirms the drift notice
        row targets the worker's recorded caller.
        """
        blueprint = tmp_path / "bp_route.md"
        blueprint.write_text("# Route test")
        sha = _sha(blueprint)

        # Create a third-party receiver
        with prod_db.begin() as db:
            db.add(TerminalModel(
                id="33333333",
                tmux_session="cao-test",
                tmux_window="third-party",
                provider="kiro_cli",
                agent_profile="developer",
                lifecycle_generation=1,
            ))
            db.add(TerminalModel(
                id="44444444",
                tmux_session="cao-test",
                tmux_window="drift-route-worker",
                provider="kiro_cli",
                agent_profile="kiro_reviewer",
                caller_id="aaaaaaaa",  # caller is the supervisor
                lifecycle_generation=1,
            ))
            db.add(AuthorityPinModel(
                task_key="44444444",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="aaaaaaaa",
                frozen=True,
            ))

        blueprint.write_text("# Drifted for route test")

        with (
            patch(
                "cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed"
            ),
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
        ):
            # Send to third-party receiver (33333333), not the supervisor
            response = api_client.post(
                "/terminals/33333333/inbox/messages",
                params={"sender_id": "44444444", "message": "drift payload"},
            )

        data = response.json()
        assert data["success"] is False
        assert data["error"]["code"] == "frozen_pin_drift"

        # The drift notice must go to the CALLER (aaaaaaaa), not the receiver (33333333)
        with prod_db() as db:
            notice = db.query(InboxModel).filter(
                InboxModel.sender_id == "cao-system:drift:44444444"
            ).first()
            assert notice is not None
            assert notice.receiver_id == "aaaaaaaa", (
                "Drift notice must route to recorded caller, not message receiver"
            )
            assert "[FROZEN-PIN-DRIFT" in notice.message

    def test_drift_dedup_prevents_duplicate_notice(
        self, prod_db, tmp_path, api_client, monkeypatch
    ):
        """M7: dedup check prevents a second drift notice from being created.

        Mutant M7 removes the dedup logic, allowing unlimited drift notices.
        This test sends two drifted messages and asserts only one notice row exists.
        """
        blueprint = tmp_path / "bp_dedup.md"
        blueprint.write_text("# For dedup")
        sha = _sha(blueprint)

        with prod_db.begin() as db:
            db.add(TerminalModel(
                id="55555555",
                tmux_session="cao-test",
                tmux_window="dedup-worker",
                provider="kiro_cli",
                agent_profile="kiro_reviewer",
                caller_id="aaaaaaaa",
                lifecycle_generation=1,
            ))
            db.add(AuthorityPinModel(
                task_key="55555555",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="aaaaaaaa",
                frozen=True,
            ))

        blueprint.write_text("# Drifted for dedup")

        with (
            patch(
                "cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed"
            ),
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
        ):
            # First drift
            r1 = api_client.post(
                "/terminals/aaaaaaaa/inbox/messages",
                params={"sender_id": "55555555", "message": "first drift attempt"},
            )
            assert r1.json()["error"]["code"] == "frozen_pin_drift"

            # Second drift — should be deduped
            r2 = api_client.post(
                "/terminals/aaaaaaaa/inbox/messages",
                params={"sender_id": "55555555", "message": "second drift attempt"},
            )
            assert r2.json()["error"]["code"] == "frozen_pin_drift_already_notified"

        # Only ONE notice row in the DB
        with prod_db() as db:
            notices = db.query(InboxModel).filter(
                InboxModel.sender_id == "cao-system:drift:55555555"
            ).all()
            assert len(notices) == 1

    def test_system_sender_bypasses_validation(
        self, prod_db, tmp_path, api_client, monkeypatch
    ):
        """M15: Non-terminal senders bypass frozen-pin validation entirely.

        Mutant M15 removes the terminal-sender classification check, causing
        system senders to be validated. This test patches the production-bound
        validate_frozen_pins symbol and asserts it is NOT called for a non-
        terminal sender, killing M15 (removing the classification guard causes
        the mock to be invoked).
        """
        # Use the supervisor terminal as receiver (it already exists)
        with (
            patch(
                "cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed"
            ),
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
            patch("cli_agent_orchestrator.api.main.get_backend") as mock_backend,
            patch(
                "cli_agent_orchestrator.services.authority_pin_service.validate_frozen_pins"
            ) as mock_vfp,
        ):
            mock_backend.return_value.session_exists.return_value = True
            mock_backend.return_value.get_history.return_value = ""
            response = api_client.post(
                "/terminals/aaaaaaaa/inbox/messages",
                params={
                    "sender_id": "watchdog:aaaaaaaa",  # NOT a terminal principal
                    "message": "system keepalive",
                },
            )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["success"] is True

        # validate_frozen_pins must NOT be called for non-terminal senders —
        # this kills M15 (removing classification causes the mock to fire)
        mock_vfp.assert_not_called()

        # Message stored without any attestation block
        with prod_db() as db:
            row = db.query(InboxModel).filter_by(id=data["message_id"]).first()
            assert row is not None
            assert "[FROZEN-PIN-ATTESTATION" not in row.message
            assert row.message == "system keepalive"

    def test_endpoint_placement_before_mailbox_branch(
        self, prod_db, tmp_path, api_client, monkeypatch
    ):
        """M14: Validation happens BEFORE the mb_ routing branch.

        Mutant M14 moves validation after the mb_/direct branch split, breaking
        mailbox-routed sends. This test sends a drifted message to a mailbox
        receiver and verifies drift is still caught.
        """
        blueprint = tmp_path / "bp_mb.md"
        blueprint.write_text("# For mailbox placement")
        sha = _sha(blueprint)

        with prod_db.begin() as db:
            db.add(TerminalModel(
                id="66666666",
                tmux_session="cao-test",
                tmux_window="mb-drift-worker",
                provider="kiro_cli",
                agent_profile="kiro_reviewer",
                caller_id="aaaaaaaa",
                lifecycle_generation=1,
            ))
            db.add(AuthorityPinModel(
                task_key="66666666",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="aaaaaaaa",
                frozen=True,
            ))

        blueprint.write_text("# Drifted for mb_ route")

        with (
            patch(
                "cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed"
            ),
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_logical_inbox_message"
            ) as mock_logical,
        ):
            # Target a mailbox receiver — validation must fire BEFORE mb_ branch
            response = api_client.post(
                "/terminals/mb_aabbccdd/inbox/messages",
                params={"sender_id": "66666666", "message": "stale via mailbox"},
            )

        data = response.json()
        assert data["success"] is False
        assert data["error"]["code"] == "frozen_pin_drift"
        # The mailbox function was never called (drift returned early)
        mock_logical.assert_not_called()

    def test_drift_caller_gone_returns_structured_error(
        self, prod_db, tmp_path, api_client, monkeypatch
    ):
        """M6 edge: drift when caller_id terminal no longer exists returns error."""
        blueprint = tmp_path / "bp_orphan.md"
        blueprint.write_text("# Orphan test")
        sha = _sha(blueprint)

        with prod_db.begin() as db:
            db.add(TerminalModel(
                id="77777777",
                tmux_session="cao-test",
                tmux_window="orphan-worker",
                provider="kiro_cli",
                agent_profile="kiro_reviewer",
                caller_id="99999999",  # non-existent caller
                lifecycle_generation=1,
            ))
            db.add(AuthorityPinModel(
                task_key="77777777",
                file_path=str(blueprint),
                sha256=sha,
                version=1,
                registered_by="aaaaaaaa",
                frozen=True,
            ))

        blueprint.write_text("# Drifted — orphan caller")

        with (
            patch(
                "cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed"
            ),
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
            patch("cli_agent_orchestrator.api.main.get_backend") as mock_backend,
        ):
            mock_backend.return_value.session_exists.return_value = True
            mock_backend.return_value.get_history.return_value = ""
            response = api_client.post(
                "/terminals/aaaaaaaa/inbox/messages",
                params={"sender_id": "77777777", "message": "orphan callback"},
            )

        data = response.json()
        assert data["success"] is False
        assert data["error"]["code"] == "frozen_pin_drift_no_caller"


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 3: terminal_service.create_terminal with authority_files
#           — kills M9
# ═════════════════════════════════════════════════════════════════════════════


class TestTerminalServiceInitialMessage:
    """Production terminal_service.create_terminal prepends FROZEN-AUTHORITY-PINS."""

    def test_deferred_initial_message_gains_frozen_authority_pins_block(
        self, prod_db, tmp_path, monkeypatch
    ):
        """M9: [FROZEN-AUTHORITY-PINS] block prepended to deferred initial_message.

        Mutant M9 removes the block construction from terminal_service.create_terminal.
        This test calls the REAL terminal_service.create_terminal with infrastructure
        mocks (tmux, provider) and captures the initial_message passed to
        _schedule_deferred_init to verify the block is present.
        """
        from cli_agent_orchestrator.services import terminal_service

        blueprint = tmp_path / "bp_initial.md"
        blueprint.write_text("# Task blueprint")
        sha = _sha(blueprint)
        authority_files = [{"file_path": str(blueprint), "sha256": sha}]

        captured_initial_message = {}

        original_schedule = terminal_service._schedule_deferred_init

        def capturing_schedule(
            provider_instance,
            terminal_id,
            initial_message,
            initial_message_orchestration_type,
            registry,
            **kwargs,
        ):
            captured_initial_message["value"] = initial_message
            # Don't actually schedule; just capture
            return None

        # Mock infrastructure: backend (tmux), provider, profile loading
        mock_backend = MagicMock()
        mock_backend.session_exists.return_value = True
        mock_backend.window_exists.return_value = False
        mock_backend.create_window.return_value = "win_eeeeffff"
        mock_backend.pipe_pane.return_value = None
        mock_backend.supports_event_inbox.return_value = False

        mock_provider = MagicMock()
        mock_provider.supports_seed_resume_identity = False
        mock_provider.supports_reauth_rebind = False
        mock_provider.shell_baseline = "/bin/bash"

        monkeypatch.setattr(terminal_service, "get_backend", lambda: mock_backend)
        monkeypatch.setattr(terminal_service, "_schedule_deferred_init", capturing_schedule)
        monkeypatch.setattr(
            terminal_service, "require_provider_admitted", lambda _: None
        )
        monkeypatch.setattr(
            terminal_service.provider_manager,
            "create_provider",
            lambda *a, **kw: mock_provider,
        )
        monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: "eeeeffff")
        monkeypatch.setattr(terminal_service, "generate_window_name", lambda *a, **kw: "win_eeeeffff")
        monkeypatch.setattr(
            terminal_service, "load_agent_profile", lambda _: None
        )
        monkeypatch.setattr(
            terminal_service.fifo_manager, "create_reader", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            terminal_service.fifo_manager, "has_reader", lambda _: False
        )
        monkeypatch.setattr(
            terminal_service.fifo_manager, "stop_reader", lambda _: None
        )
        # Mock disk space check
        monkeypatch.setattr(terminal_service, "_preflight_disk_space", lambda *a, **kw: None)

        result = asyncio.run(
            terminal_service.create_terminal(
                provider="kiro_cli",
                agent_profile="kiro_reviewer",
                session_name="cao-test",
                new_session=False,
                working_directory=str(tmp_path),
                caller_id="aaaaaaaa",
                initial_message="Implement feature X according to the blueprint.",
                authority_files=authority_files,
                defer_init=True,
            )
        )

        assert "value" in captured_initial_message, (
            "initial_message must reach _schedule_deferred_init"
        )
        msg = captured_initial_message["value"]
        assert "[FROZEN-AUTHORITY-PINS]" in msg
        assert "[/FROZEN-AUTHORITY-PINS]" in msg
        assert f"path={blueprint}" in msg
        assert f"sha256={sha}" in msg
        # Original message is still present after the block
        assert "Implement feature X according to the blueprint." in msg
        # Block comes BEFORE the message
        block_pos = msg.index("[FROZEN-AUTHORITY-PINS]")
        task_pos = msg.index("Implement feature X")
        assert block_pos < task_pos


# ═════════════════════════════════════════════════════════════════════════════
# N1: logger.debug assertion for best-effort exception path
# ═════════════════════════════════════════════════════════════════════════════


class TestN1BestEffortLogging:
    """N1: Pin validation crash logs debug with exc_info=True."""

    def test_exception_in_pin_validation_logs_debug_with_exc_info(
        self, prod_db, tmp_path, api_client, monkeypatch, caplog
    ):
        """N1: unexpected exception in F129 block -> logger.debug(..., exc_info=True).

        Verifies the logger call fires with exc_info when pin validation crashes
        (e.g., DB unavailable mid-validation).
        """
        # Set up a terminal sender so the code enters the validation path
        with prod_db.begin() as db:
            db.add(TerminalModel(
                id="88888888",
                tmux_session="cao-test",
                tmux_window="crash-worker",
                provider="kiro_cli",
                agent_profile="developer",
                caller_id="aaaaaaaa",
                lifecycle_generation=1,
            ))

        # Make validate_frozen_pins raise an unexpected error
        with (
            patch(
                "cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed"
            ),
            patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
            patch("cli_agent_orchestrator.api.main.get_backend") as mock_backend,
            patch(
                "cli_agent_orchestrator.services.authority_pin_service.validate_frozen_pins",
                side_effect=RuntimeError("simulated DB crash"),
            ),
            caplog.at_level(logging.DEBUG, logger="cli_agent_orchestrator.api.main"),
        ):
            mock_backend.return_value.session_exists.return_value = True
            mock_backend.return_value.get_history.return_value = ""
            response = api_client.post(
                "/terminals/aaaaaaaa/inbox/messages",
                params={"sender_id": "88888888", "message": "test after crash"},
            )

        # Message still delivered (best-effort semantics)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["success"] is True

        # Logger.debug fired with exc_info
        f129_records = [
            r for r in caplog.records
            if "F129 pin validation skipped" in r.message
        ]
        assert len(f129_records) >= 1, (
            "logger.debug must fire when pin validation crashes"
        )
        assert f129_records[0].exc_info is not None, (
            "exc_info=True must be passed to logger.debug"
        )
