"""F631 slice 1 (D4, reap half) — the resume key reaches the reap caller.

Blueprint §1 records the defect: ``delete_terminal`` "returns a cascade dict
with no resume key", so the 19:19Z operator who reaped two gate reviewers had
nothing to resume them by. Slice 1 closes that on the two links above the DB
writer (whose own arm lives in ``test/clients/test_f631_terminal_identity.py``):

* ``_delete_terminal_under_lease`` reads the key off the DB result, and
* ``_delete_terminal_inner`` reports it on the cascade's reaped entry.

Mock harness mirrors ``test/services/test_f255_deferred_cleanup_delete.py``
(under-lease) and ``test/services/test_f167_scoped_quiesce.py`` (cascade).
"""

from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.terminal_service import delete_terminal


def _lane(terminal_id, caller_id=None):
    return {
        "id": terminal_id,
        "tmux_session": "cao-f631",
        "tmux_window": f"codex_dev-{terminal_id}",
        "provider": "codex",
        "agent_profile": "codex_dev",
        "caller_id": caller_id,
        "lifecycle": "ephemeral",
        "init_state": "ready",
        "provider_session_id": None,
        "metadata": {},
    }


# ── link 1: _delete_terminal_under_lease propagates the DB writer's key ─────


@patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent")
@patch("cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease")
@patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
@patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
@patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
@patch("cli_agent_orchestrator.backends.registry._backend")
@patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
def test_under_lease_returns_the_db_resume_key(
    mock_get_metadata,
    mock_tmux,
    mock_provider_manager,
    mock_fifo_manager,
    mock_status_monitor,
    mock_validate_lease,
    mock_db_delete,
):
    from cli_agent_orchestrator.services.terminal_service import _delete_terminal_under_lease

    terminal_id = "f631lane"
    mock_get_metadata.return_value = _lane(terminal_id, caller_id="supervis1")
    mock_tmux.get_history.return_value = ""
    mock_tmux.get_pane_working_directory.return_value = "/home/chao/repo"
    mock_tmux.kill_window.return_value = None
    mock_tmux.window_liveness.return_value = "gone"
    mock_tmux.stop_pipe_pane.return_value = None
    mock_fifo_manager.stop_reader.return_value = None
    mock_status_monitor.unregister.return_value = None
    mock_provider_manager.cleanup_provider.return_value = True
    mock_db_delete.return_value = {
        "terminal_deleted": True,
        "intent_deleted": True,
        "resume_key": "uuid-under-lease",
    }

    with patch("cli_agent_orchestrator.services.terminal_service.worktree_service") as mock_wt:
        mock_wt.parse_worktree_path.return_value = None
        result = _delete_terminal_under_lease(terminal_id, "fake-lease")

    assert result["resume_key"] == "uuid-under-lease"
    # Negative control: the pre-existing keys of this contract are unchanged.
    assert result["terminal_deleted"] is True
    assert result["rollback_kill_uncertain"] is False


@patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent")
@patch("cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease")
@patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
@patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
@patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
@patch("cli_agent_orchestrator.backends.registry._backend")
@patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
def test_under_lease_tolerates_a_result_without_a_resume_key(
    mock_get_metadata,
    mock_tmux,
    mock_provider_manager,
    mock_fifo_manager,
    mock_status_monitor,
    mock_validate_lease,
    mock_db_delete,
):
    """The F138 non-durable-force branch fabricates a result that never reached
    the DB writer, so the read must be a ``.get``, not an index."""
    from cli_agent_orchestrator.services.terminal_service import _delete_terminal_under_lease

    terminal_id = "f631lan2"
    mock_get_metadata.return_value = _lane(terminal_id, caller_id="supervis1")
    mock_tmux.get_history.return_value = ""
    mock_tmux.get_pane_working_directory.return_value = "/home/chao/repo"
    mock_tmux.kill_window.return_value = None
    mock_tmux.window_liveness.return_value = "gone"
    mock_tmux.stop_pipe_pane.return_value = None
    mock_fifo_manager.stop_reader.return_value = None
    mock_status_monitor.unregister.return_value = None
    mock_provider_manager.cleanup_provider.return_value = True
    mock_db_delete.return_value = {"terminal_deleted": True, "intent_deleted": True}

    with patch("cli_agent_orchestrator.services.terminal_service.worktree_service") as mock_wt:
        mock_wt.parse_worktree_path.return_value = None
        result = _delete_terminal_under_lease(terminal_id, "fake-lease")

    assert result["resume_key"] is None
    assert result["terminal_deleted"] is True


# ── link 2: the cascade reports the key on each reaped entry ────────────────


def _arm_cascade(monkeypatch, terminals, under_lease):
    from cli_agent_orchestrator.services.terminal_guard_service import DeletionClassification

    by_id = {row["id"]: row for row in terminals}
    monkeypatch.setattr(terminal_service, "quiesce_deferred_terminal_sync", lambda tid, **kw: None)
    monkeypatch.setattr(terminal_service, "list_terminals_by_session", lambda _s: terminals)
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", by_id.get)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_guard_service.classify_deletion",
        lambda tid, force=False: DeletionClassification(True),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
        lambda tid, force=False: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_lifecycle_lease."
        "acquire_session_lifecycle_exclusive_blocking",
        lambda _s, timeout_s=5.0: "lease",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_lease",
        lambda _l: None,
    )
    monkeypatch.setattr(terminal_service, "has_deferred_init", lambda tid: False)
    monkeypatch.setattr(terminal_service, "_delete_terminal_under_lease", under_lease)
    monkeypatch.setattr(
        terminal_service,
        "status_monitor",
        MagicMock(
            get_boundary_observation=MagicMock(
                return_value=MagicMock(status=MagicMock(value="idle"))
            )
        ),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
        lambda tid: MagicMock(terminal_id=tid),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease", lambda _t: None
    )
    monkeypatch.setattr(terminal_service, "get_backend", lambda: MagicMock())


def test_cascade_reports_the_resume_key_on_the_reaped_entry(monkeypatch):
    root = _lane("rootroot")
    child = _lane("childaaa", caller_id="rootroot")
    _arm_cascade(
        monkeypatch,
        [root, child],
        lambda tid, token, **kw: {"terminal_deleted": True, "resume_key": f"uuid-{tid}"},
    )

    result = delete_terminal("childaaa", caller_id="rootroot")

    entries = {row["id"]: row for row in result["reaped"]}
    assert entries["childaaa"]["resume_key"] == "uuid-childaaa"
    # Negative control: the cascade's existing shape is untouched.
    assert entries["childaaa"]["status"] == "reaped"
    assert result["skipped"] == [] and result["uncertain"] == []


def test_cascade_omits_the_key_for_a_lane_that_has_none(monkeypatch):
    """A lane with no captured provider session reports no key at all — the
    entry must not carry a null one that reads like a resumable handle."""
    root = _lane("rootroo2")
    child = _lane("childbbb", caller_id="rootroo2")
    _arm_cascade(
        monkeypatch,
        [root, child],
        lambda tid, token, **kw: {"terminal_deleted": True, "resume_key": None},
    )

    result = delete_terminal("childbbb", caller_id="rootroo2")

    entry = result["reaped"][0]
    assert entry["id"] == "childbbb"
    assert "resume_key" not in entry
    assert entry["status"] == "reaped"
