"""Unit tests for the herdr inbox registration/unregistration wiring in terminal_service.

These tests verify the wiring between terminal lifecycle functions and the herdr inbox
service, in isolation from real tmux/herdr:

- create_terminal: registers the new terminal with the herdr inbox service when one is
  available (herdr path), skips registration when the service is None (tmux path), and
  never lets a registration failure tear down an otherwise-successful terminal.
- delete_terminal: unregisters from the herdr inbox service when available and is a
  no-op against the service when it is None.

Note on signature: create_terminal's real signature is
    create_terminal(provider, agent_profile, session_name=None, new_session=False,
                    working_directory=None, allowed_tools=None, registry=None)
so the keyword arguments below follow the implementation, not the (stale) task brief.
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import ForkContext, Terminal
from cli_agent_orchestrator.services.terminal_service import (
    create_terminal,
    delete_terminal,
)
from cli_agent_orchestrator.services import epoch_recovery_service
from cli_agent_orchestrator.services import terminal_service

# Fixed identifiers used across the tests so assertions can be exact.
TERMINAL_ID = "term-abc123"
SESSION_NAME = "cao-test-session"
WINDOW_NAME = "developer-wxyz"
PANE_ID = "%42"

# Module path prefix for patch targets (all dependencies are imported into the
# terminal_service namespace, so they are patched there, not at their origin).
_TS = "cli_agent_orchestrator.services.terminal_service."


@pytest.fixture
def quarantine_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'quarantine.db'}")
    local = sessionmaker(bind=engine)
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", local)
    return local


@pytest.fixture
def create_mocks():
    """Patch every external dependency of create_terminal and yield the mocks.

    get_backend() returns a process-wide singleton in production, so a single MagicMock
    backs every get_backend() call here; session_exists/create_window/get_pane_id/pipe_pane
    are configured on that one object via ``get_backend.return_value``.

    Defaults model the happy path on the herdr branch:
    - existing tmux session (session_exists -> True), so new_session=False succeeds
    - load_agent_profile -> None, which skips allowed-tools resolution and model lookup
    - provider creates cleanly; shell_baseline is a MagicMock (not str) so it is treated
      as None and update_terminal_shell_command is never called
    - get_herdr_inbox_service -> a live mock service
    """
    with contextlib.ExitStack() as stack:

        def p(name):
            return stack.enter_context(patch(_TS + name))

        m = SimpleNamespace(
            get_herdr_inbox_service=p("get_herdr_inbox_service"),
            get_backend=p("get_backend"),
            db_create_terminal=p("db_create_terminal"),
            provider_manager=p("provider_manager"),
            generate_terminal_id=p("generate_terminal_id"),
            generate_session_name=p("generate_session_name"),
            generate_window_name=p("generate_window_name"),
            load_agent_profile=p("load_agent_profile"),
            build_skill_catalog=p("build_skill_catalog"),
            dispatch_plugin_event=p("dispatch_plugin_event"),
            update_terminal_shell_command=p("update_terminal_shell_command"),
            # TERMINAL_LOG_DIR is a Path; a MagicMock supports `/` (__truediv__),
            # .touch(), and str(), so log-file setup becomes a no-op.
            TERMINAL_LOG_DIR=p("TERMINAL_LOG_DIR"),
        )

        m.generate_terminal_id.return_value = TERMINAL_ID
        m.generate_window_name.return_value = "developer-base"
        m.load_agent_profile.return_value = None

        backend = m.get_backend.return_value
        backend.session_exists.return_value = True
        backend.create_window.return_value = WINDOW_NAME
        backend.get_pane_id.return_value = PANE_ID
        # Herdr-style backend: event-inbox based, so the FIFO/pipe-pane setup is
        # skipped and inbox delivery goes through the herdr registration below.
        backend.supports_event_inbox.return_value = True

        # create_terminal awaits provider.initialize(); make it a coroutine.
        provider_instance = m.provider_manager.create_provider.return_value
        provider_instance.initialize = AsyncMock(return_value=True)

        service = MagicMock()
        m.get_herdr_inbox_service.return_value = service
        m.service = service
        m.backend = backend

        yield m


class TestCreateTerminalHerdrRegistration:
    """create_terminal -> herdr inbox registration wiring."""

    @pytest.mark.asyncio
    async def test_create_terminal_registers_with_herdr_inbox(self, create_mocks):
        """When a herdr inbox service exists, the new terminal is registered with it."""
        # Arrange
        m = create_mocks

        # Act
        terminal = await create_terminal(
            provider="claude_code",
            agent_profile="developer",
            session_name=SESSION_NAME,
        )

        # Assert: pane id resolved for the created window, then registered with is_kiro=False
        m.backend.get_pane_id.assert_called_once_with(TERMINAL_ID, SESSION_NAME, WINDOW_NAME)
        m.service.register_terminal.assert_called_once_with(TERMINAL_ID, PANE_ID, False)
        assert isinstance(terminal, Terminal)
        assert terminal.id == TERMINAL_ID

    @pytest.mark.asyncio
    async def test_create_terminal_no_registration_when_service_none(self, create_mocks):
        """On the tmux path (service is None) no registration is attempted."""
        # Arrange
        m = create_mocks
        m.get_herdr_inbox_service.return_value = None

        # Act
        terminal = await create_terminal(
            provider="claude_code",
            agent_profile="developer",
            session_name=SESSION_NAME,
        )

        # Assert: guard short-circuits before pane lookup or registration
        m.backend.get_pane_id.assert_not_called()
        m.service.register_terminal.assert_not_called()
        assert isinstance(terminal, Terminal)
        assert terminal.id == TERMINAL_ID

    @pytest.mark.asyncio
    async def test_create_terminal_registration_failure_does_not_kill_terminal(self, create_mocks):
        """A registration failure is swallowed; the terminal is still created and returned."""
        # Arrange: pane id lookup blows up (e.g. TerminalNotFoundError) inside the
        # registration block. The inner try/except must contain it.
        m = create_mocks
        m.backend.get_pane_id.side_effect = RuntimeError("pane not found")

        # Act
        terminal = await create_terminal(
            provider="claude_code",
            agent_profile="developer",
            session_name=SESSION_NAME,
        )

        # Assert: creation succeeded and no exception propagated
        assert isinstance(terminal, Terminal)
        assert terminal.id == TERMINAL_ID
        # registration never completed (failed before the register call)
        m.service.register_terminal.assert_not_called()
        # the outer failure-cleanup path was NOT triggered -> terminal not torn down
        m.backend.kill_session.assert_not_called()
        m.provider_manager.cleanup_provider.assert_not_called()

    @pytest.mark.parametrize("site,code,post_publish", [
        ("window", "window_create_failed", False),
        ("fifo_reader", "fifo_create_failed", False),
        ("pipe_pane", "fifo_create_failed", False),
        ("db", "db_publish_failed", False),
        ("context", "context_build_failed", True),
        ("provider", "provider_construct_failed", True),
        ("init_timeout", "initialize_timeout", True),
        ("init_failure", "initialize_failed", True),
        ("capture_ambiguous", "session_capture_ambiguous", True),
        ("capture_mismatch", "session_capture_mismatch", True),
        ("artifact", "artifact_invalid", True),
        ("identity", "identity_persist_failed", True),
        ("herdr", "herdr_register_failed", True),
    ])
    @pytest.mark.asyncio
    async def test_d5_real_leased_creation_site_normalization(
        self, create_mocks, site, code, post_publish,
    ):
        m = create_mocks
        token = SimpleNamespace(terminal_id=TERMINAL_ID)
        metadata = {
            "id": TERMINAL_ID, "tmux_session": SESSION_NAME, "tmux_window": WINDOW_NAME,
            "provider": "claude_code", "agent_profile": "developer",
            "allowed_tools": None, "caller_id": None, "provider_session_id": "native-u",
        }
        m.backend.window_liveness.return_value = "gone"
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch(
                "cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease"
            ))
            get_metadata = stack.enter_context(patch(_TS + "get_terminal_metadata", return_value=metadata))
            delete_row = stack.enter_context(patch(_TS + "db_delete_terminal", return_value=True))
            stack.enter_context(patch(_TS + "delete_warm_intent_for_terminal", create=True))
            fifo = stack.enter_context(patch(_TS + "fifo_manager"))
            persist = stack.enter_context(patch(_TS + "_persist_provider_runtime_identity"))

            if site == "window":
                m.backend.create_window.side_effect = RuntimeError("boom")
            elif site in {"fifo_reader", "pipe_pane"}:
                m.backend.supports_event_inbox.return_value = False
                if site == "fifo_reader":
                    fifo.create_reader.side_effect = RuntimeError("boom")
                else:
                    m.backend.pipe_pane.side_effect = RuntimeError("boom")
            elif site == "db":
                m.db_create_terminal.side_effect = RuntimeError("boom")
            elif site == "context":
                m.load_agent_profile.return_value = SimpleNamespace(
                    sessionBrief="required", skills=None, allowedTools=None,
                    mcpServers=None, role=None, model=None,
                )
                stack.enter_context(patch(
                    "cli_agent_orchestrator.services.session_manifest_service.build_session_manifest",
                    side_effect=RuntimeError("boom"),
                ))
            elif site == "provider":
                m.provider_manager.create_provider.side_effect = RuntimeError("boom")
            elif site == "init_timeout":
                m.provider_manager.create_provider.return_value.initialize.side_effect = TimeoutError("boom")
            elif site == "init_failure":
                m.provider_manager.create_provider.return_value.initialize.side_effect = RuntimeError("boom")
            elif site == "capture_ambiguous":
                persist.side_effect = RuntimeError("session_capture_ambiguous")
            elif site == "capture_mismatch":
                persist.side_effect = RuntimeError("session_capture_mismatch")
            elif site == "artifact":
                persist.side_effect = RuntimeError("session_artifact_invalid")
            elif site == "identity":
                persist.side_effect = RuntimeError("terminal_identity_persist_failed")
            elif site == "herdr":
                m.service.register_terminal.side_effect = RuntimeError("boom")

            with pytest.raises(Exception) as caught:
                await create_terminal(
                    provider="claude_code", agent_profile="developer",
                    session_name=SESSION_NAME, terminal_id=TERMINAL_ID,
                    lease_token=token, strict_backend_registration=True,
                )

        assert epoch_recovery_service._normalize_creation_error(caught.value) == code
        if post_publish:
            get_metadata.assert_called_once_with(TERMINAL_ID)
            delete_row.assert_called_once_with(TERMINAL_ID)
        else:
            delete_row.assert_not_called()

    @pytest.mark.asyncio
    async def test_uncertain_leased_rollback_retains_runtime_authority(self, create_mocks):
        m = create_mocks
        token = SimpleNamespace(terminal_id=TERMINAL_ID)
        metadata = {
            "id": TERMINAL_ID, "tmux_session": SESSION_NAME, "tmux_window": WINDOW_NAME,
            "provider": "claude_code", "agent_profile": "developer",
            "allowed_tools": None, "caller_id": None,
        }
        m.service.register_terminal.side_effect = RuntimeError("register failed")
        m.backend.window_liveness.return_value = "live"
        m.provider_manager.get_provider.return_value = m.provider_manager.create_provider.return_value
        with patch(_TS + "get_terminal_metadata", return_value=metadata) as get_metadata, \
             patch(_TS + "status_monitor") as monitor, \
             patch(_TS + "db_delete_terminal") as delete_row, \
             patch(_TS + "list_terminals_by_provider_session_id",
                   return_value=[metadata]), \
             patch("cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease"), \
             patch("cli_agent_orchestrator.clients.database.quarantine_terminal_owner",
                   return_value=True) as quarantine:
            with pytest.raises(RuntimeError, match="rollback_kill_uncertain"):
                await create_terminal(
                    provider="claude_code", agent_profile="developer",
                    session_name=SESSION_NAME, terminal_id=TERMINAL_ID,
                    lease_token=token, strict_backend_registration=True,
                )
            assert terminal_service.provider_session_owner("native-u") == {
                "state": "live", "terminal_id": TERMINAL_ID,
            }

        m.db_create_terminal.assert_called_once()
        get_metadata.assert_called_once_with(TERMINAL_ID)
        quarantine.assert_called_once_with(
            TERMINAL_ID, None, "rollback_kill_uncertain"
        )
        assert m.provider_manager.get_provider(TERMINAL_ID) is m.provider_manager.create_provider.return_value
        monitor.clear_terminal.assert_not_called()
        m.service.unregister_terminal.assert_not_called()
        delete_row.assert_not_called()
        m.provider_manager.cleanup_provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_pre_identity_quarantine_blocks_second_epoch_owner(
        self, create_mocks, quarantine_db, monkeypatch,
    ):
        m = create_mocks
        token = SimpleNamespace(terminal_id=TERMINAL_ID)
        source_uuid = "source-native-u"
        m.db_create_terminal.side_effect = database.create_terminal
        m.backend.window_liveness.return_value = "live"
        with patch(_TS + "get_terminal_metadata", side_effect=database.get_terminal_metadata), \
             patch(_TS + "db_delete_terminal", side_effect=database.delete_terminal) as delete_row, \
             patch(_TS + "_persist_provider_runtime_identity",
                   side_effect=RuntimeError("session_capture_ambiguous")), \
             patch("cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease"):
            with pytest.raises(RuntimeError, match="rollback_kill_uncertain"):
                await create_terminal(
                    provider="claude_code", agent_profile="developer",
                    session_name=SESSION_NAME, terminal_id=TERMINAL_ID,
                    lease_token=token,
                    fork_context=ForkContext(
                        mode="resume", session_uuid=source_uuid,
                        base_name="base", provider="claude_code",
                        initial_preamble="",
                    ),
                )

        retained = database.get_terminal_metadata(TERMINAL_ID)
        assert retained["provider_session_id"] == source_uuid
        assert retained["recovery_state"] == "rebind_failed"
        assert retained["recovery_error"] == "rollback_kill_uncertain"
        delete_row.assert_not_called()

        database.register_provider_session(
            name="base", provider="claude_code", session_uuid=source_uuid,
            cwd="/tmp", agent_profile="developer", dirty_hashes="{}",
            source_terminal_id="old", session_name=SESSION_NAME,
        )
        monkeypatch.setattr(epoch_recovery_service, "get_backend", lambda: m.backend)
        monkeypatch.setattr(epoch_recovery_service, "_artifact_exists", lambda _: True)
        monkeypatch.setattr(epoch_recovery_service, "load_agent_profile", lambda _: SimpleNamespace())
        create_again = AsyncMock()
        monkeypatch.setattr(epoch_recovery_service, "create_terminal", create_again)
        result = await epoch_recovery_service.recover_epoch(SESSION_NAME)
        assert result["results"][0]["status"] == "skipped_live_owner"
        create_again.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_leased_resume_uuid_is_published_before_provider_start(
        self, create_mocks, quarantine_db,
    ):
        m = create_mocks
        source_uuid = "source-before-provider"
        m.db_create_terminal.side_effect = database.create_terminal
        observed = []
        def fail_provider(*_args, **_kwargs):
            observed.append(database.get_terminal_metadata(TERMINAL_ID)["provider_session_id"])
            raise RuntimeError("provider failed")
        m.provider_manager.create_provider.side_effect = fail_provider
        m.backend.window_liveness.return_value = "gone"
        with patch(_TS + "get_terminal_metadata", side_effect=database.get_terminal_metadata), \
             patch(_TS + "db_delete_terminal", side_effect=database.delete_terminal), \
             patch("cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease"):
            with pytest.raises(RuntimeError, match="provider_construct_failed"):
                await create_terminal(
                    provider="claude_code", agent_profile="developer",
                    session_name=SESSION_NAME, terminal_id=TERMINAL_ID,
                    lease_token=SimpleNamespace(terminal_id=TERMINAL_ID),
                    fork_context=ForkContext(
                        mode="resume", session_uuid=source_uuid, base_name="base",
                        provider="claude_code", initial_preamble="",
                    ),
                )
        assert observed == [source_uuid]

    @pytest.mark.asyncio
    async def test_quarantine_db_exception_is_closed_and_retains_authority(
        self, create_mocks, quarantine_db,
    ):
        m = create_mocks
        source_uuid = "source-quarantine-error"
        m.db_create_terminal.side_effect = database.create_terminal
        m.backend.window_liveness.return_value = "live"
        with patch(_TS + "get_terminal_metadata", side_effect=database.get_terminal_metadata), \
             patch(_TS + "db_delete_terminal", side_effect=database.delete_terminal) as delete_row, \
             patch(_TS + "_persist_provider_runtime_identity",
                   side_effect=RuntimeError("session_capture_ambiguous")), \
             patch("cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease"), \
             patch("cli_agent_orchestrator.clients.database.quarantine_terminal_owner",
                   side_effect=RuntimeError("database is locked")):
            with pytest.raises(RuntimeError) as caught:
                await create_terminal(
                    provider="claude_code", agent_profile="developer",
                    session_name=SESSION_NAME, terminal_id=TERMINAL_ID,
                    lease_token=SimpleNamespace(terminal_id=TERMINAL_ID),
                    fork_context=ForkContext(
                        mode="resume", session_uuid=source_uuid, base_name="base",
                        provider="claude_code", initial_preamble="",
                    ),
                )
        assert str(caught.value) == "quarantine_persist_failed"
        assert epoch_recovery_service._normalize_creation_error(caught.value) == "quarantine_persist_failed"
        assert epoch_recovery_service._result(
            "base", "resume_failed", error_code="quarantine_persist_failed"
        )["retryable"] is False
        retained = database.get_terminal_metadata(TERMINAL_ID)
        assert retained["provider_session_id"] == source_uuid
        delete_row.assert_not_called()
        m.provider_manager.cleanup_provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_terminal_kiro_provider_sets_is_kiro_true(self, create_mocks):
        """A kiro_cli provider registers with is_kiro=True."""
        # Arrange
        m = create_mocks

        # Act
        await create_terminal(
            provider=ProviderType.KIRO_CLI.value,
            agent_profile="developer",
            session_name=SESSION_NAME,
        )

        # Assert
        m.service.register_terminal.assert_called_once_with(TERMINAL_ID, PANE_ID, True)


@pytest.fixture
def delete_mocks():
    """Patch delete_terminal's dependencies and yield the mocks.

    get_terminal_metadata -> None so the scrollback-snapshot / stop-pipe-pane / kill-window
    block (which needs the backend) is skipped, keeping these tests focused on the herdr
    unregistration wiring. db_delete_terminal -> True so delete_terminal returns True.
    """
    with contextlib.ExitStack() as stack:

        def p(name):
            return stack.enter_context(patch(_TS + name))

        m = SimpleNamespace(
            get_herdr_inbox_service=p("get_herdr_inbox_service"),
            get_terminal_metadata=p("get_terminal_metadata"),
            provider_manager=p("provider_manager"),
            db_delete_terminal=p("db_delete_terminal"),
            dispatch_plugin_event=p("dispatch_plugin_event"),
        )

        m.get_terminal_metadata.return_value = None
        m.db_delete_terminal.return_value = True

        service = MagicMock()
        m.get_herdr_inbox_service.return_value = service
        m.service = service

        yield m


class TestDeleteTerminalHerdrUnregistration:
    """delete_terminal -> herdr inbox unregistration wiring."""

    def test_delete_terminal_unregisters_from_herdr_inbox(self, delete_mocks):
        """When a herdr inbox service exists, the terminal is unregistered from it."""
        # Arrange
        m = delete_mocks

        # Act
        result = delete_terminal(TERMINAL_ID)

        # Assert
        m.service.unregister_terminal.assert_called_once_with(TERMINAL_ID)
        assert result is True

    def test_delete_terminal_no_unregistration_when_service_none(self, delete_mocks):
        """On the tmux path (service is None) no unregister call is made.

        If the None-guard were missing, the code would call unregister_terminal on None
        and raise AttributeError; a clean True return proves the guard holds.
        """
        # Arrange
        m = delete_mocks
        m.get_herdr_inbox_service.return_value = None

        # Act
        result = delete_terminal(TERMINAL_ID)

        # Assert
        m.service.unregister_terminal.assert_not_called()
        assert result is True
