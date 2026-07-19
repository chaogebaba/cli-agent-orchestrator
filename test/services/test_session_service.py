"""Tests for the session service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.services.session_service import (
    canonical_session_env,
    create_session,
    delete_session,
    get_session,
    list_sessions,
)
from cli_agent_orchestrator.services import session_service


def test_canonical_session_env_uses_working_directory(tmp_path):
    result = canonical_session_env(str(tmp_path), {"KEEP": "yes"})
    assert result == {
        "KEEP": "yes",
        "CAO_ARTIFACTS_DIR": str(tmp_path.resolve() / "tmp" / "orch"),
    }


def test_canonical_session_env_accepts_absolute_override(tmp_path):
    override = tmp_path / "artifacts"
    result = canonical_session_env("/ignored", {"CAO_ARTIFACTS_DIR": str(override)})
    assert result["CAO_ARTIFACTS_DIR"] == str(override.resolve())


@pytest.mark.parametrize("override", ["", "tmp/orch"])
def test_canonical_session_env_rejects_non_absolute_override(override):
    with pytest.raises(ValueError, match="artifacts_dir_not_absolute"):
        canonical_session_env("/repo", {"CAO_ARTIFACTS_DIR": override})


class TestCreateSession:
    """Tests for create_session function."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    @patch("cli_agent_orchestrator.services.session_service.resolve_provider")
    async def test_create_session_resolves_provider_when_omitted(
        self, mock_resolve, mock_create_terminal, mock_dispatch
    ):
        """When provider is None, resolve_provider is called and its result forwarded."""
        mock_resolve.return_value = "claude_code"
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        await create_session(provider=None, agent_profile="my_agent")

        mock_resolve.assert_called_once_with("my_agent", fallback_provider="kiro_cli")
        assert mock_create_terminal.call_args.kwargs["provider"] == "claude_code"

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    @patch("cli_agent_orchestrator.services.session_service.resolve_provider")
    async def test_create_session_uses_explicit_provider(
        self, mock_resolve, mock_create_terminal, mock_dispatch
    ):
        """When provider is explicitly passed, resolve_provider is NOT called."""
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        await create_session(provider="kiro_cli", agent_profile="my_agent")

        mock_resolve.assert_not_called()
        assert mock_create_terminal.call_args.kwargs["provider"] == "kiro_cli"

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    async def test_create_session_forwards_canonical_root_once(
        self, mock_create_terminal, mock_dispatch, tmp_path
    ):
        mock_terminal = MagicMock(session_name="cao-test")
        mock_create_terminal.return_value = mock_terminal

        with patch(
            "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
            return_value=None,
        ):
            await create_session(
                provider="codex",
                agent_profile="developer",
                working_directory=str(tmp_path),
                env_vars={"KEEP": "yes"},
            )

        assert mock_create_terminal.call_args.kwargs["env_vars"] == {
            "KEEP": "yes",
            "CAO_ARTIFACTS_DIR": str(tmp_path.resolve() / "tmp" / "orch"),
        }

    @pytest.mark.asyncio
    async def test_wpq11_fresh_supervisor_publication_has_no_synthetic_push(
        self, monkeypatch, tmp_path
    ):
        from datetime import datetime

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients import database
        from cli_agent_orchestrator.clients.database import (
            Base,
            InboxDeliveryAttemptModel,
            InboxModel,
            MailboxIncarnationModel,
            MailboxModel,
            TerminalModel,
        )
        from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
        from cli_agent_orchestrator.services import mailbox_service

        engine = create_engine(
            f"sqlite:///{tmp_path / 'fresh-supervisor.sqlite'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        monkeypatch.setattr(database, "engine", engine)
        database._migrate_mailbox_columns()
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr(database, "SessionLocal", sessions)
        monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)

        now = datetime.now()
        with sessions.begin() as db:
            db.add_all(
                [
                    TerminalModel(
                        id="11111111",
                        tmux_session="cao-wpq11",
                        tmux_window="old",
                        provider="codex",
                        lifecycle_generation=3,
                        init_state="ready",
                    ),
                    TerminalModel(
                        id="22222222",
                        tmux_session="cao-wpq11",
                        tmux_window="new",
                        provider="codex",
                        lifecycle_generation=0,
                        init_state="ready",
                    ),
                ]
            )
            db.add(
                MailboxModel(
                    id="mb_aaaaaaaa",
                    session_name="cao-wpq11",
                    role="supervisor",
                    current_terminal_id="11111111",
                    generation=1,
                    consumed_through_id=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                MailboxIncarnationModel(
                    mailbox_id="mb_aaaaaaaa",
                    generation=1,
                    terminal_id="11111111",
                    published_at=now,
                )
            )
            history = InboxModel(
                sender_id="99999999",
                receiver_id="11111111",
                logical_receiver_id="mb_aaaaaaaa",
                enqueue_generation=1,
                message="delivered history",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.DELIVERED.value,
                created_at=now,
            )
            db.add(history)
            db.flush()
            history_id = int(history.id)
            for index in range(105):
                axis = index % 3
                db.add(
                    InboxModel(
                        sender_id=f"{index:08x}",
                        receiver_id="22222222" if axis == 2 else "11111111",
                        logical_receiver_id="mb_aaaaaaaa" if axis == 0 else None,
                        enqueue_generation=1 if axis == 0 else 0 if axis == 2 else 3,
                        message=f"backlog-{index}",
                        orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                        status=MessageStatus.PENDING.value,
                        created_at=now,
                    )
                )

        terminal = MagicMock(id="22222222", session_name="cao-wpq11")
        create = AsyncMock(return_value=terminal)
        backend_submit = MagicMock()
        provider_lookup = MagicMock()
        monkeypatch.setattr(session_service, "create_terminal", create)
        monkeypatch.setattr(
            session_service, "load_agent_profile", lambda _name: MagicMock(role="supervisor")
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            backend_submit,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            provider_lookup,
        )
        monkeypatch.setattr(session_service, "dispatch_plugin_event", MagicMock())

        await create_session(
            provider="codex",
            agent_profile="code_supervisor",
            session_name="cao-wpq11",
        )

        backend_submit.assert_not_called()
        provider_lookup.assert_not_called()
        with sessions() as db:
            assert db.query(InboxModel).filter_by(status=MessageStatus.PENDING.value).count() == 0
            parked = db.query(InboxModel).filter_by(status=MessageStatus.PARKED.value).all()
            assert len(parked) == 105
            assert all(row.owner_receiver_id is not None for row in parked)
            assert all(row.owner_generation is not None for row in parked)
            assert db.get(MailboxModel, "mb_aaaaaaaa").consumed_through_id == history_id
            assert db.query(InboxDeliveryAttemptModel).count() == 0
        engine.dispose()

    @pytest.mark.asyncio
    async def test_wpq11_fresh_worker_submits_exactly_one_caller_task(self, monkeypatch):
        from cli_agent_orchestrator.models.inbox import OrchestrationType
        from cli_agent_orchestrator.services import terminal_service

        provider = MagicMock()
        provider.initialize = AsyncMock()
        provider.shell_baseline = None
        caller_submit = MagicMock()

        async def run_blocking(_tid, _generation, _kind, _operation, function, *args, **kwargs):
            kwargs.pop("deadline", None)
            return function(*args, **kwargs), None

        monkeypatch.setattr(terminal_service, "_tracked_blocking", run_blocking)
        monkeypatch.setattr(
            terminal_service, "_prepare_provider_runtime_identity", lambda *_a, **_k: None
        )
        monkeypatch.setattr(terminal_service, "send_input", caller_submit)
        monkeypatch.setattr(
            terminal_service, "_mark_ready_if_generation_current", AsyncMock(return_value=True)
        )

        terminal_service._schedule_deferred_init(
            provider,
            "22222222",
            "caller task",
            OrchestrationType.ASSIGN,
            None,
            caller_snapshot={
                "caller_id": "11111111",
                "tmux_session": "cao-wpq11",
                "init_deadline_s": 60.0,
            },
        )
        task = terminal_service._deferred_tasks_by_terminal["22222222"].task
        await task

        provider.initialize.assert_awaited_once()
        caller_submit.assert_called_once_with(
            "22222222",
            "caller task",
            registry=None,
            sender_id="11111111",
            orchestration_type=OrchestrationType.ASSIGN,
        )


class TestListSessions:
    """Tests for list_sessions function."""

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_success(self, mock_get_backend):
        """Test listing sessions successfully."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-session1", "name": "Session 1"},
            {"id": "cao-session2", "name": "Session 2"},
            {"id": "other-session", "name": "Other"},
        ]

        result = list_sessions()

        assert len(result) == 2
        assert all(s["id"].startswith("cao-") for s in result)

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_empty(self, mock_get_backend):
        """Test listing sessions when none exist."""
        mock_get_backend.return_value.list_sessions.return_value = []

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_no_cao_sessions(self, mock_get_backend):
        """Test listing sessions when no CAO sessions exist."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "other-session1", "name": "Other 1"},
            {"id": "other-session2", "name": "Other 2"},
        ]

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_error(self, mock_get_backend):
        """Test listing sessions with error."""
        mock_get_backend.return_value.list_sessions.side_effect = Exception("Tmux error")

        result = list_sessions()

        assert result == []


class TestGetSession:
    """Tests for get_session function."""

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_success(self, mock_get_backend, mock_list_terminals):
        """Test getting session successfully."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-test", "name": "Test Session"}
        ]
        mock_list_terminals.return_value = [{"id": "terminal1", "session": "cao-test"}]

        result = get_session("cao-test")

        assert result["session"]["id"] == "cao-test"
        assert len(result["terminals"]) == 1
        mock_get_backend.return_value.session_exists.assert_called_once_with("cao-test")

    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor.get_status")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_enriches_terminals_with_live_status(
        self, mock_get_backend, mock_list_terminals, mock_get_status
    ):
        """Each terminal should carry its live status (consumed by the web UI
        and the cao-ops-mcp get_session_info tool an external supervisor polls)."""
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = [{"id": "cao-test"}]
        mock_list_terminals.return_value = [
            {"id": "term-a", "tmux_session": "cao-test"},
            {"id": "term-b", "tmux_session": "cao-test"},
        ]
        mock_get_status.side_effect = lambda tid: {
            "term-a": TerminalStatus.PROCESSING,
            "term-b": TerminalStatus.COMPLETED,
        }[tid]

        result = get_session("cao-test")

        assert result["terminals"][0]["status"] == "processing"
        assert result["terminals"][1]["status"] == "completed"

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_not_found(self, mock_get_backend):
        """Test getting non-existent session."""
        mock_get_backend.return_value.session_exists.return_value = False

        with pytest.raises(ValueError, match="Session 'cao-nonexistent' not found"):
            get_session("cao-nonexistent")

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_not_in_list(self, mock_get_backend):
        """Test getting session that exists but not in list."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = []

        with pytest.raises(ValueError, match="Session 'cao-test' not found"):
            get_session("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_error(self, mock_get_backend):
        """Test getting session with error."""
        mock_get_backend.return_value.session_exists.side_effect = Exception("Tmux error")

        with pytest.raises(Exception, match="Tmux error"):
            get_session("cao-test")


class TestDeleteSession:
    @patch("cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease")
    @patch("cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_mixed_session_protection_is_all_or_nothing(
        self, mock_backend, mock_list, mock_preflight, mock_delete
    ):
        mock_backend.return_value.session_exists.return_value = True
        mock_list.return_value = [{"id": "plain"}, {"id": "protected"}]
        mock_preflight.side_effect = [None, ValueError("protected")]

        with pytest.raises(ValueError, match="protected"):
            delete_session("cao-mixed")

        assert mock_preflight.call_count == 2
        mock_delete.assert_not_called()
        mock_backend.return_value.kill_session.assert_not_called()

    """Tests for delete_session function."""

    @patch("cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_success(
        self,
        mock_get_backend,
        mock_list_terminals,
        mock_delete_terminal,
    ):
        """Test deleting session successfully.

        delete_session delegates per-terminal teardown (FIFO reader, status
        buffer, provider, DB) to terminal_service.delete_terminal, then kills
        the backend session and returns the Dict result shape.
        """
        mock_get_backend.return_value.session_exists.side_effect = [True, False, False]
        mock_list_terminals.return_value = [
            {"id": "terminal1"},
            {"id": "terminal2"},
        ]

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        # Each terminal is torn down via the event-driven delete_terminal path.
        assert mock_delete_terminal.call_count == 2
        assert [call.args[0] for call in mock_delete_terminal.call_args_list] == [
            "terminal1",
            "terminal2",
        ]

    @patch("cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_when_backend_session_already_gone(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal
    ):
        """Backend session already gone — delete_session should not raise and not
        call kill_session, but still tear down each terminal via delete_terminal."""
        mock_get_backend.return_value.session_exists.return_value = False
        mock_list_terminals.return_value = [{"id": "terminal1"}]

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_not_called()
        assert mock_delete_terminal.call_args.args[0] == "terminal1"

    @patch("cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_no_terminals(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal
    ):
        """Test deleting session with no terminals."""
        mock_get_backend.return_value.session_exists.side_effect = [True, False, False]
        mock_list_terminals.return_value = []

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        mock_delete_terminal.assert_not_called()

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_error(self, mock_get_backend, mock_list_terminals):
        """Test deleting session with error."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.side_effect = Exception("Database error")

        with pytest.raises(Exception, match="Database error"):
            delete_session("cao-test")

    @patch("cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_continues_when_terminal_cleanup_fails(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal
    ):
        """Test that delete_session continues even when terminal teardown fails for some terminals."""
        mock_get_backend.return_value.session_exists.side_effect = [True, False, False]
        mock_list_terminals.return_value = [
            {"id": "terminal1"},
            {"id": "terminal2"},
            {"id": "terminal3"},
        ]

        # First terminal teardown fails, others succeed
        mock_delete_terminal.side_effect = [
            Exception("Terminal teardown error for terminal1"),
            None,  # terminal2 succeeds
            None,  # terminal3 succeeds
        ]

        result = delete_session("cao-test")

        # Session should still be deleted despite per-terminal teardown failure
        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        # All three terminal teardowns were attempted
        assert mock_delete_terminal.call_count == 3

    @patch("cli_agent_orchestrator.services.session_service.time.sleep")
    @patch("cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_retries_when_session_survives_initial_kill(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal, mock_sleep
    ):
        """If tmux still reports the session after graceful cleanup, kill it again."""
        backend = mock_get_backend.return_value
        backend.session_exists.side_effect = [True, True, False, False]
        mock_list_terminals.return_value = []

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        assert backend.kill_session.call_count == 2
        backend.kill_session.assert_any_call("cao-test")
        mock_sleep.assert_called_once()

    @patch("cli_agent_orchestrator.services.session_service.time.sleep")
    @patch("cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_raises_when_session_remains_after_retries(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal, mock_sleep
    ):
        """A still-live tmux session is an error so callers do not relaunch into 400."""
        backend = mock_get_backend.return_value
        backend.session_exists.return_value = True
        mock_list_terminals.return_value = []

        with pytest.raises(RuntimeError, match="still exists after teardown"):
            delete_session("cao-test")

        assert backend.kill_session.call_count == 6

    @patch("cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_cleans_up_each_terminal(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal
    ):
        """Test that delete_session tears down every terminal in the session via delete_terminal."""
        mock_get_backend.return_value.session_exists.side_effect = [True, False, False]
        mock_list_terminals.return_value = [
            {"id": "term-aaa"},
            {"id": "term-bbb"},
            {"id": "term-ccc"},
            {"id": "term-ddd"},
        ]

        result = delete_session("cao-multi-terminal")

        assert result == {"deleted": ["cao-multi-terminal"], "errors": []}
        # Verify delete_terminal was called for each terminal with the correct ID
        assert mock_delete_terminal.call_count == 4
        assert [call.args[0] for call in mock_delete_terminal.call_args_list] == [
            "term-aaa",
            "term-bbb",
            "term-ccc",
            "term-ddd",
        ]
