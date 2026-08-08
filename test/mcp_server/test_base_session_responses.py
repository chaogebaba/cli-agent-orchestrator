"""Provider-session MCP response serialization tests."""

from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.mcp_server.server import (
    list_base_sessions,
    mark_base_ready,
    unregister_base,
)
from cli_agent_orchestrator.services import fork_context_service
from cli_agent_orchestrator.services.fork_context_service import SnapshotDelta, SnapshotEntry


def test_ac11_mark_ready_computes_metrics_before_snapshot_state_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured = SnapshotDelta(
        "head",
        (
            SnapshotEntry("x", "absent"),
            SnapshotEntry("y", "unhashable"),
        ),
    )
    monkeypatch.setattr(
        fork_context_service,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "provider": "grok_cli",
            "provider_session_id": "session",
            "working_directory": str(tmp_path),
            "agent_profile": "dev",
            "tmux_session": "cao-s",
            "tmux_window": "w",
        },
    )
    monkeypatch.setattr(fork_context_service, "snapshot", lambda _cwd: captured)
    monkeypatch.setattr(
        fork_context_service,
        "register_provider_session",
        lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(
        fork_context_service,
        "update_terminal_provider_session_id",
        lambda *_args: None,
    )

    row = fork_context_service.mark_ready("terminal", "base", None)

    assert row["_entry_count"] == 2
    assert row["_projected_manifest_bytes"] == len(b"absent - x\nunhashable - y")


@pytest.mark.asyncio
async def test_mark_base_ready_replaces_dirty_hashes_with_count(monkeypatch):
    row = {
        "name": "base",
        "dirty_hashes": '{"one.py":"abc","two.py":null}',
        "_entry_count": 2,
        "_projected_manifest_bytes": 42,
    }
    monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
    terminal_response = patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    with (
        patch("cli_agent_orchestrator.services.fork_context_service.mark_ready", return_value=row),
        terminal_response as mock_get,
    ):
        mock_get.return_value.json.return_value = {"caller_id": None}
        response = await mark_base_ready("base")

    assert response["base"]["dirty_file_count"] == 2
    assert "dirty_hashes" not in response["base"]
    assert response["entry_count"] == 2
    assert response["projected_manifest_bytes"] == 42
    assert "manifest_budget_warning" not in response
    assert response["callback"] == {"status": "not_applicable"}


@pytest.mark.asyncio
async def test_e3_mark_base_ready_threads_anchor_kind(monkeypatch):
    row = {
        "name": "root",
        "kind": "anchor",
        "dirty_hashes": "{}",
        "_entry_count": 0,
        "_projected_manifest_bytes": 0,
    }
    monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
    with (
        patch(
            "cli_agent_orchestrator.services.fork_context_service.mark_ready",
            return_value=row,
        ) as mark,
        patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get,
    ):
        mock_get.return_value.json.return_value = {"caller_id": None}
        response = await mark_base_ready("root", summary=None, kind="anchor")

    assert response["base"]["kind"] == "anchor"
    mark.assert_called_once_with("a1b2c3d4", "root", None, "anchor")


@pytest.mark.asyncio
async def test_mark_base_ready_notifies_recorded_caller(monkeypatch):
    row = {
        "name": "infra",
        "dirty_hashes": "{}",
        "_entry_count": 0,
        "_projected_manifest_bytes": 0,
    }
    monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
    with (
        patch("cli_agent_orchestrator.services.fork_context_service.mark_ready", return_value=row),
        patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get,
        patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox") as mock_inbox,
    ):
        mock_get.return_value.json.return_value = {"caller_id": "caller-1"}
        response = await mark_base_ready("infra", "loaded context")

    assert response["success"] is True
    assert response["callback"] == {"status": "delivered"}
    mock_inbox.assert_called_once_with("caller-1", "Base 'infra' ready: loaded context")


@pytest.mark.asyncio
async def test_mark_base_ready_reports_callback_failure_without_failing_mark(monkeypatch):
    row = {
        "name": "infra",
        "dirty_hashes": "{}",
        "_entry_count": 0,
        "_projected_manifest_bytes": 0,
    }
    monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
    with (
        patch("cli_agent_orchestrator.services.fork_context_service.mark_ready", return_value=row),
        patch(
            "cli_agent_orchestrator.mcp_server.server.requests.get",
            side_effect=RuntimeError("offline"),
        ),
    ):
        response = await mark_base_ready("infra", "loaded context")

    assert response["success"] is True
    assert response["callback"] == {"status": "failed", "error": "offline"}


@pytest.mark.asyncio
async def test_list_base_sessions_replaces_dirty_hashes_with_count():
    rows = [
        {"name": "dirty", "dirty_hashes": '{"one.py":"abc"}'},
        {"name": "clean", "dirty_hashes": None},
    ]
    with patch(
        "cli_agent_orchestrator.services.fork_context_service.list_bases", return_value=rows
    ):
        response = await list_base_sessions()

    assert [row["dirty_file_count"] for row in response["bases"]] == [1, 0]
    assert all("dirty_hashes" not in row for row in response["bases"])


@pytest.mark.asyncio
async def test_t2h_all_base_serializers_filter_nested_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "live.py").write_text("live", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".git").write_text("", encoding="utf-8")
    row = {
        "name": "base",
        "cwd": str(tmp_path),
        "dirty_hashes": '{"live.py":"abc","nested/missing.py":"def"}',
        "_entry_count": 2,
        "_projected_manifest_bytes": 40,
    }
    monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")

    with (
        patch(
            "cli_agent_orchestrator.services.fork_context_service.mark_ready",
            return_value=row,
        ),
        patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get,
    ):
        mock_get.return_value.json.return_value = {"caller_id": None}
        marked = await mark_base_ready("base")
    with patch(
        "cli_agent_orchestrator.services.fork_context_service.list_bases",
        return_value=[row],
    ):
        listed = await list_base_sessions()
    with patch(
        "cli_agent_orchestrator.services.fork_context_service.retire",
        return_value=row,
    ):
        retired = await unregister_base("base")

    assert marked["base"]["dirty_file_count"] == 1
    assert listed["bases"][0]["dirty_file_count"] == 1
    assert retired["base"]["dirty_file_count"] == 1


@pytest.mark.asyncio
async def test_t2h_serializer_marker_oserror_counts_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    nested = tmp_path / "nested"
    nested.mkdir()
    row = {
        "name": "base",
        "cwd": str(tmp_path),
        "dirty_hashes": '{"nested/missing.py":"def"}',
        "_entry_count": 1,
        "_projected_manifest_bytes": 24,
    }
    monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
    original_stat = Path.stat

    def marker_stat(path: Path, *args, **kwargs):
        if path == nested / ".git":
            raise OSError("marker unreadable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", marker_stat)

    with (
        patch(
            "cli_agent_orchestrator.services.fork_context_service.mark_ready",
            return_value=row,
        ),
        patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get,
    ):
        mock_get.return_value.json.return_value = {"caller_id": None}
        response = await mark_base_ready("base")

    assert response["base"]["dirty_file_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("projected_bytes", "warns"),
    [(5_999, False), (6_000, False), (6_001, True)],
)
async def test_ac11_manifest_warning_is_strictly_above_half_the_cap(
    monkeypatch, projected_bytes: int, warns: bool
):
    row = {
        "name": "base",
        "dirty_hashes": "{}",
        "_entry_count": 3,
        "_projected_manifest_bytes": projected_bytes,
    }
    monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
    with (
        patch(
            "cli_agent_orchestrator.services.fork_context_service.mark_ready",
            return_value=row,
        ),
        patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get,
    ):
        mock_get.return_value.json.return_value = {"caller_id": None}
        response = await mark_base_ready("base")

    assert response["entry_count"] == 3
    assert response["projected_manifest_bytes"] == projected_bytes
    assert ("manifest_budget_warning" in response) is warns


@pytest.mark.asyncio
async def test_f80_mark_base_ready_surfaces_dirty_file_count(monkeypatch):
    """F80: MCP response includes dirty_file_count and manifest_warning."""
    row = {
        "name": "base",
        "dirty_hashes": '{"a.py":"abc","b.py":"def","c.py":null}',
        "_entry_count": 3,
        "_projected_manifest_bytes": 100,
    }
    monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
    with (
        patch("cli_agent_orchestrator.services.fork_context_service.mark_ready", return_value=row),
        patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get,
    ):
        mock_get.return_value.json.return_value = {"caller_id": None}
        response = await mark_base_ready("base")

    assert response["success"] is True
    assert response["dirty_file_count"] == 3
    assert response["manifest_warning"] is None  # 100 is well below 80% of 12000


@pytest.mark.asyncio
async def test_f80_mark_base_ready_manifest_warning_near_cap(monkeypatch):
    """F80: manifest_warning set when projected manifest exceeds 80% cap."""
    from cli_agent_orchestrator.services.base_digest_service import MAX_DIGEST_BYTES

    near_cap_bytes = int(MAX_DIGEST_BYTES * 0.81)
    row = {
        "name": "base",
        "dirty_hashes": '{"a.py":"abc"}',
        "_entry_count": 90,
        "_projected_manifest_bytes": near_cap_bytes,
    }
    monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
    with (
        patch("cli_agent_orchestrator.services.fork_context_service.mark_ready", return_value=row),
        patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get,
    ):
        mock_get.return_value.json.return_value = {"caller_id": None}
        response = await mark_base_ready("base")

    assert response["manifest_warning"] == "near budget cap"
    assert response["dirty_file_count"] == 90
