"""Service-level base retirement behavior."""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.fork_context_service import (
    ForkContextError,
    list_bases,
    resolve_base,
    retire,
)


def test_retire_is_thin_and_makes_no_backend_or_terminal_call():
    row = {"name": "base", "status": "retired"}
    with patch(
        "cli_agent_orchestrator.services.fork_context_service.retire_provider_session",
        return_value=row,
    ) as retire_db, patch(
        "cli_agent_orchestrator.backends.registry.get_backend"
    ) as backend, patch(
        "cli_agent_orchestrator.services.terminal_service.delete_terminal"
    ) as delete_terminal:
        assert retire("base") == row
    retire_db.assert_called_once_with("base")
    backend.assert_not_called()
    delete_terminal.assert_not_called()


def test_list_bases_uses_ready_only_registry_rows():
    with patch(
        "cli_agent_orchestrator.services.fork_context_service.list_ready_provider_sessions",
        return_value=[],
    ) as ready_rows:
        assert list_bases() == []
    ready_rows.assert_called_once_with()


def test_e3_anchor_is_typed_unforkable_and_absent_from_forkable_listing():
    anchor = {"name": "root", "kind": "anchor"}
    base = {"name": "forkable", "kind": "base"}
    with patch(
        "cli_agent_orchestrator.services.fork_context_service.get_ready_provider_session",
        return_value=anchor,
    ):
        with pytest.raises(ForkContextError, match="anchor_not_forkable:root"):
            resolve_base("root")

    with patch(
        "cli_agent_orchestrator.services.fork_context_service.list_ready_provider_sessions",
        return_value=[anchor, base],
    ), patch(
        "cli_agent_orchestrator.services.fork_context_service.staleness",
        return_value=([], "fresh"),
    ):
        assert [row["name"] for row in list_bases()] == ["forkable"]
