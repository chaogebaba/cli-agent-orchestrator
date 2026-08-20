"""Tests for F335 readopt_service — registry self-heal.

Isolation: uses the test conftest's in-memory DB (CAO_HOME_DIR is already
overridden to a temp dir by the top-level conftest.py). Mocks tmux
subprocess calls so these tests never touch the real tmux server.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.clients.database import (
    MailboxIncarnationModel,
    MailboxModel,
    SessionLocal,
    TerminalModel,
)
from cli_agent_orchestrator.services.readopt_service import (
    ReadoptPlan,
    ReadoptResult,
    _WINDOW_NAME_RE,
    apply_readopt,
    scan_for_orphans,
)


class TestWindowNamePattern:
    """Ensure the regex matches the expected naming convention."""

    def test_valid_window_name(self):
        m = _WINDOW_NAME_RE.match("developer-a1b2c3d4")
        assert m is not None
        assert m.group(1) == "developer"
        assert m.group(2) == "a1b2c3d4"

    def test_valid_hyphenated_profile(self):
        m = _WINDOW_NAME_RE.match("code-supervisor-deadbeef")
        assert m is not None
        assert m.group(1) == "code-supervisor"
        assert m.group(2) == "deadbeef"

    def test_uppercase_hex_rejected(self):
        """Terminal IDs are lowercase hex only."""
        m = _WINDOW_NAME_RE.match("developer-A1B2C3D4")
        assert m is None

    def test_too_short_id(self):
        m = _WINDOW_NAME_RE.match("developer-a1b2c3")
        assert m is None

    def test_too_long_id(self):
        m = _WINDOW_NAME_RE.match("developer-a1b2c3d4e5")
        assert m is None

    def test_no_dash(self):
        m = _WINDOW_NAME_RE.match("developera1b2c3d4")
        assert m is None


class TestScanForOrphans:
    """Test scan logic with mocked tmux output."""

    def test_empty_tmux(self):
        result = scan_for_orphans(tmux_windows=[])
        assert result.planned == []
        assert result.skipped_test == []

    def test_non_cao_sessions_ignored(self):
        windows = [
            ("my-session", "developer-a1b2c3d4"),
            ("user-stuff", "some-window"),
        ]
        result = scan_for_orphans(tmux_windows=windows)
        assert result.planned == []

    def test_test_sessions_skipped(self):
        windows = [
            ("cao-test-fixtures", "developer-a1b2c3d4"),
            ("cao-test-e2e", "reviewer-deadbeef"),
        ]
        result = scan_for_orphans(tmux_windows=windows)
        assert result.planned == []
        assert len(result.skipped_test) == 2

    @patch("cli_agent_orchestrator.services.readopt_service._resolve_provider_for_profile")
    @patch("cli_agent_orchestrator.services.readopt_service._is_supervisor_profile")
    def test_orphan_detected(self, mock_is_sup, mock_provider):
        mock_provider.return_value = "kiro_cli"
        mock_is_sup.return_value = False

        windows = [
            ("cao-my-session", "developer-a1b2c3d4"),
        ]
        result = scan_for_orphans(tmux_windows=windows)
        assert len(result.planned) == 1
        plan = result.planned[0]
        assert plan.terminal_id == "a1b2c3d4"
        assert plan.tmux_session == "cao-my-session"
        assert plan.agent_profile == "developer"
        assert plan.provider == "kiro_cli"
        assert plan.lifecycle == "ephemeral"
        assert plan.needs_mailbox is False

    @patch("cli_agent_orchestrator.services.readopt_service._resolve_provider_for_profile")
    @patch("cli_agent_orchestrator.services.readopt_service._is_supervisor_profile")
    def test_supervisor_gets_mailbox(self, mock_is_sup, mock_provider):
        mock_provider.return_value = "kiro_cli"
        mock_is_sup.return_value = True

        windows = [
            ("cao-prod", "code_supervisor-cafe0123"),
        ]
        result = scan_for_orphans(tmux_windows=windows)
        assert len(result.planned) == 1
        plan = result.planned[0]
        assert plan.lifecycle == "sticky"
        assert plan.needs_mailbox is True

    @patch("cli_agent_orchestrator.services.readopt_service._resolve_provider_for_profile")
    @patch("cli_agent_orchestrator.services.readopt_service._is_supervisor_profile")
    def test_existing_terminal_skipped(self, mock_is_sup, mock_provider):
        """Terminals already in the DB should be skipped."""
        mock_provider.return_value = "kiro_cli"
        mock_is_sup.return_value = False

        # Insert a terminal so it exists
        now = datetime.now(timezone.utc)
        with SessionLocal.begin() as db:
            db.add(
                TerminalModel(
                    id="abcd1234",
                    tmux_session="cao-existing",
                    tmux_window="developer-abcd1234",
                    provider="kiro_cli",
                    agent_profile="developer",
                    lifecycle="ephemeral",
                    init_state="ready",
                    last_active=now,
                )
            )

        windows = [
            ("cao-existing", "developer-abcd1234"),
        ]
        result = scan_for_orphans(tmux_windows=windows)
        assert result.planned == []
        assert "abcd1234" in result.skipped_existing


class TestApplyReadopt:
    """Test that apply_readopt writes the correct rows."""

    def test_apply_creates_terminal_row(self):
        plan = ReadoptPlan(
            terminal_id="ff001122",
            tmux_session="cao-apply-test",
            tmux_window="developer-ff001122",
            agent_profile="developer",
            provider="kiro_cli",
            lifecycle="ephemeral",
            needs_mailbox=False,
        )
        result = ReadoptResult(planned=[plan])
        apply_readopt(result)

        assert "ff001122" in result.applied
        assert result.errors == []

        with SessionLocal() as db:
            row = db.query(TerminalModel).filter_by(id="ff001122").one()
            assert row.tmux_session == "cao-apply-test"
            assert row.agent_profile == "developer"
            assert row.provider == "kiro_cli"
            assert row.lifecycle == "ephemeral"
            assert row.init_state == "ready"

    def test_apply_creates_mailbox_for_supervisor(self):
        plan = ReadoptPlan(
            terminal_id="ee334455",
            tmux_session="cao-sup-test",
            tmux_window="code_supervisor-ee334455",
            agent_profile="code_supervisor",
            provider="kiro_cli",
            lifecycle="sticky",
            needs_mailbox=True,
        )
        result = ReadoptResult(planned=[plan])
        apply_readopt(result)

        assert "ee334455" in result.applied
        assert result.errors == []

        with SessionLocal() as db:
            # Check terminal
            term = db.query(TerminalModel).filter_by(id="ee334455").one()
            assert term.lifecycle == "sticky"

            # Check mailbox
            mbx = db.query(MailboxModel).filter_by(id="mbx-ee334455").one()
            assert mbx.session_name == "cao-sup-test"
            assert mbx.role == "supervisor"
            assert mbx.current_terminal_id == "ee334455"

            # Check incarnation
            inc = (
                db.query(MailboxIncarnationModel)
                .filter_by(mailbox_id="mbx-ee334455", generation=1)
                .one()
            )
            assert inc.terminal_id == "ee334455"

    def test_apply_skips_if_appeared_between_scan_and_apply(self):
        """Simulate a race: terminal appears between scan and apply."""
        now = datetime.now(timezone.utc)
        with SessionLocal.begin() as db:
            db.add(
                TerminalModel(
                    id="race0001",
                    tmux_session="cao-race",
                    tmux_window="developer-race0001",
                    provider="kiro_cli",
                    agent_profile="developer",
                    lifecycle="ephemeral",
                    init_state="ready",
                    last_active=now,
                )
            )

        plan = ReadoptPlan(
            terminal_id="race0001",
            tmux_session="cao-race",
            tmux_window="developer-race0001",
            agent_profile="developer",
            provider="kiro_cli",
            lifecycle="ephemeral",
            needs_mailbox=False,
        )
        result = ReadoptResult(planned=[plan])
        apply_readopt(result)

        # Should have been skipped, not applied
        assert "race0001" not in result.applied
        assert "race0001" in result.skipped_existing

    def test_apply_never_touches_existing_rows(self):
        """Even if planned, existing rows must not be modified."""
        now = datetime.now(timezone.utc)
        with SessionLocal.begin() as db:
            db.add(
                TerminalModel(
                    id="keep0001",
                    tmux_session="cao-original",
                    tmux_window="developer-keep0001",
                    provider="codex",
                    agent_profile="developer",
                    lifecycle="ephemeral",
                    init_state="ready",
                    last_active=now,
                )
            )

        plan = ReadoptPlan(
            terminal_id="keep0001",
            tmux_session="cao-different",
            tmux_window="reviewer-keep0001",
            agent_profile="reviewer",
            provider="kiro_cli",
            lifecycle="sticky",
            needs_mailbox=True,
        )
        result = ReadoptResult(planned=[plan])
        apply_readopt(result)

        # Original row should be unchanged
        with SessionLocal() as db:
            row = db.query(TerminalModel).filter_by(id="keep0001").one()
            assert row.tmux_session == "cao-original"
            assert row.provider == "codex"
            assert row.agent_profile == "developer"
