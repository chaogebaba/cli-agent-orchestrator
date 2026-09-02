"""F264: Unit test for list_terminals_by_session ObjectDeletedError hardening."""

from unittest.mock import MagicMock, patch, PropertyMock

import pytest


def test_list_terminals_by_session_skips_stale_rows():
    """Stale/zombie rows that raise on attribute access are skipped, not crash."""
    from sqlalchemy.orm.exc import ObjectDeletedError

    # Create a mock terminal that raises ObjectDeletedError on attribute access
    good_terminal = MagicMock()
    good_terminal.id = "good-1234"
    good_terminal.tmux_session = "cao-test"
    good_terminal.tmux_window = "w0"
    good_terminal.provider = "grok_cli"
    good_terminal.agent_profile = "grok_dev"
    good_terminal.working_directory = "/tmp"
    good_terminal.allowed_tools = None
    good_terminal.shell_command = None
    good_terminal.caller_id = None
    good_terminal.caller_mailbox_id = None
    good_terminal.lifecycle = "ephemeral"
    good_terminal.reparented_from = None
    good_terminal.provider_session_id = None
    good_terminal.recovery_state = None
    good_terminal.recovery_error = None
    good_terminal.recovery_updated_at = None
    good_terminal.fallback_terminal_id = None
    good_terminal.init_state = None
    good_terminal.init_started_at = None
    good_terminal.init_owner_epoch = None
    good_terminal.init_failure_token = None
    good_terminal.init_deadline_s = None
    good_terminal.engine = None
    good_terminal.metadata_json = None
    good_terminal.last_active = None

    stale_terminal = MagicMock()
    # Make .id raise ObjectDeletedError (simulates a deleted row)
    type(stale_terminal).id = PropertyMock(
        side_effect=ObjectDeletedError(MagicMock(), "stale instance")
    )

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
        good_terminal,
        stale_terminal,
    ]

    with patch("cli_agent_orchestrator.clients.database.SessionLocal") as mock_session_local:
        mock_session_local.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=False)

        from cli_agent_orchestrator.clients.database import list_terminals_by_session

        results = list_terminals_by_session("cao-test")

    # Should get only the good terminal, stale one skipped
    assert len(results) == 1, f"Expected 1 result, got {len(results)}"
    assert results[0]["id"] == "good-1234"


def test_list_terminals_by_session_empty_session():
    """Empty session returns empty list without error."""
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

    with patch("cli_agent_orchestrator.clients.database.SessionLocal") as mock_session_local:
        mock_session_local.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=False)

        from cli_agent_orchestrator.clients.database import list_terminals_by_session

        results = list_terminals_by_session("nonexistent-session")

    assert results == []
