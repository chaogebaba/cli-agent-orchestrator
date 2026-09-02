"""F702 J4 (#473) — the fleet TUI window is created server-side at session start.

Two layers, matching the two halves of the fix:

1. ``fleet_window_service.ensure_fleet_window`` decides and creates: the
   ``CAO_FLEET_TUI`` opt-out, the absent ``[fleet]`` extra, an idempotent
   no-op when the window is already there, index-1 placement, and the total
   exception boundary that makes every failure a logged False.
2. ``session_service.create_session`` calls it for a supervisor session and
   not for a worker one, and a session start still succeeds when the window
   cannot be created.

tmux is mocked throughout — no test here starts a tmux server or a window.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.services import fleet_window_service, session_service
from cli_agent_orchestrator.services.fleet_window_service import (
    FLEET_CONSOLE_SCRIPT,
    FLEET_TUI_MODULE,
    FLEET_WINDOW_NAME,
    ensure_fleet_window,
    fleet_tui_enabled,
)
from cli_agent_orchestrator.services.session_service import create_session

CAO_FLEET_PATH = "/usr/local/bin/cao-fleet"


def _fleet_extra(present=True):
    """Patch the ``[fleet]`` extra probe: the textual spec is there, or is not."""
    return patch.object(
        fleet_window_service.importlib.util,
        "find_spec",
        MagicMock(return_value=MagicMock() if present else None),
    )


def _completed(returncode=0, stdout="", stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _tmux_runner(list_windows_result, new_window_result=None):
    """Return a ``subprocess.run`` double dispatching on the tmux subcommand."""

    def run(argv, **kwargs):
        assert argv[0] == "tmux"
        if argv[1] == "list-windows":
            return list_windows_result
        if argv[1] == "new-window":
            return new_window_result if new_window_result is not None else _completed()
        raise AssertionError(f"unexpected tmux subcommand: {argv[1]}")

    return MagicMock(side_effect=run)


def _new_window_argv(mock_run):
    for call in mock_run.call_args_list:
        argv = call.args[0]
        if argv[1] == "new-window":
            return argv
    return None


class TestOptOutFlag:
    """``CAO_FLEET_TUI`` keeps the exact semantics of the two shell guards."""

    @pytest.mark.parametrize(
        "env,expected",
        [
            (None, True),
            ({}, True),
            ({"CAO_FLEET_TUI": "1"}, True),
            ({"CAO_FLEET_TUI": "0"}, False),
            # Only the literal "0" disables — "false"/"no"/"" all stay enabled,
            # because fleet-tui-ensure.sh:12 compares against 0 and nothing else.
            ({"CAO_FLEET_TUI": "false"}, True),
            ({"CAO_FLEET_TUI": "no"}, True),
            ({"CAO_FLEET_TUI": ""}, True),
            ({"CAO_FLEET_TUI": "00"}, True),
        ],
    )
    def test_flag_semantics(self, env, expected):
        assert fleet_tui_enabled(env) is expected

    def test_opt_out_touches_neither_path_nor_tmux(self):
        with (
            patch.object(fleet_window_service.shutil, "which") as mock_which,
            _fleet_extra() as mock_find_spec,
            patch.object(fleet_window_service.subprocess, "run") as mock_run,
        ):
            assert ensure_fleet_window("cao-x", {"CAO_FLEET_TUI": "0"}) is False

        mock_which.assert_not_called()
        mock_find_spec.assert_not_called()
        mock_run.assert_not_called()


class TestWindowCreation:
    """Placement, idempotence and the exact tmux command line."""

    def test_creates_at_index_1_when_free(self):
        run = _tmux_runner(_completed(stdout="0 chao_supervisor-abc123\n"))
        with (
            patch.object(fleet_window_service.shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            patch.object(fleet_window_service.subprocess, "run", run),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is True

        argv = _new_window_argv(run)
        assert argv == [
            "tmux",
            "new-window",
            "-d",
            "-t",
            "cao-foreign:1",
            "-n",
            FLEET_WINDOW_NAME,
            f"{CAO_FLEET_PATH} --session cao-foreign",
        ]

    def test_appends_when_index_1_is_taken(self):
        run = _tmux_runner(_completed(stdout="0 supervisor-a\n1 kiro_dev-b\n"))
        with (
            patch.object(fleet_window_service.shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            patch.object(fleet_window_service.subprocess, "run", run),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is True

        argv = _new_window_argv(run)
        # Target is the bare session: appended, never renumbering the worker
        # that already holds index 1.
        assert argv[4] == "cao-foreign"

    def test_existing_fleet_window_is_left_alone(self):
        run = _tmux_runner(_completed(stdout="0 supervisor-a\n1 fleet\n"))
        with (
            patch.object(fleet_window_service.shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            patch.object(fleet_window_service.subprocess, "run", run),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

        assert _new_window_argv(run) is None

    def test_window_name_is_matched_exactly_not_by_prefix(self):
        """A window called ``fleet-notes`` is not the fleet window."""
        run = _tmux_runner(_completed(stdout="0 supervisor-a\n2 fleet-notes\n"))
        with (
            patch.object(fleet_window_service.shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            patch.object(fleet_window_service.subprocess, "run", run),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is True

        assert _new_window_argv(run) is not None


class TestNeverRaises:
    """Every failure mode is a logged False, never an exception."""

    def test_absent_console_script_is_a_no_op(self):
        """No ``cao-fleet`` on PATH at all: tmux is untouched."""
        run = MagicMock()
        with (
            patch.object(fleet_window_service.shutil, "which", return_value=None) as mock_which,
            patch.object(fleet_window_service.subprocess, "run", run),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

        mock_which.assert_called_once_with(FLEET_CONSOLE_SCRIPT)
        run.assert_not_called()

    def test_absent_fleet_extra_is_a_no_op(self):
        """The script is on PATH but textual is not: a server-only install.

        pyproject declares ``cao-fleet`` unconditionally, so PATH alone does not
        prove the extra is installed; without this probe the session would get a
        window that opens only to print an install hint and die.
        """
        run = MagicMock()
        with (
            patch.object(fleet_window_service.shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(present=False) as mock_find_spec,
            patch.object(fleet_window_service.subprocess, "run", run),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

        mock_find_spec.assert_called_once_with(FLEET_TUI_MODULE)
        run.assert_not_called()

    def test_list_windows_failure_creates_nothing(self):
        """An unknown inventory must not be guessed at."""
        run = _tmux_runner(_completed(returncode=1, stderr="no server running"))
        with (
            patch.object(fleet_window_service.shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            patch.object(fleet_window_service.subprocess, "run", run),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

        assert _new_window_argv(run) is None

    def test_new_window_failure_returns_false(self):
        run = _tmux_runner(
            _completed(stdout="0 supervisor-a\n"),
            _completed(returncode=1, stderr="can't create window"),
        )
        with (
            patch.object(fleet_window_service.shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            patch.object(fleet_window_service.subprocess, "run", run),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

    @pytest.mark.parametrize(
        "boom",
        [
            FileNotFoundError("tmux"),
            OSError("cannot fork"),
            RuntimeError("something unforeseen"),
        ],
    )
    def test_subprocess_explosion_is_swallowed(self, boom):
        with (
            patch.object(fleet_window_service.shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            patch.object(fleet_window_service.subprocess, "run", MagicMock(side_effect=boom)),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

    def test_which_explosion_is_swallowed(self):
        with patch.object(
            fleet_window_service.shutil, "which", MagicMock(side_effect=RuntimeError("boom"))
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False


def _supervisor_session_patches(terminal):
    """The mock stack a supervisor ``create_session`` needs to reach the end."""
    return (
        patch.object(session_service, "create_terminal", AsyncMock(return_value=terminal)),
        patch.object(
            session_service,
            "load_agent_profile",
            MagicMock(return_value=MagicMock(role="supervisor")),
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
            AsyncMock(return_value=None),
        ),
        patch(
            "cli_agent_orchestrator.services.mailbox_service.claim_mailbox",
            MagicMock(return_value=MagicMock(session_name="cao-f702", role="supervisor")),
        ),
        patch(
            "cli_agent_orchestrator.services.mailbox_service.publish_supervisor_incarnation",
            MagicMock(return_value={"mailbox_id": "mb-1", "generation": 1}),
        ),
        patch("cli_agent_orchestrator.services.inbox_service.inbox_service", MagicMock()),
        patch.object(session_service, "_reconcile_inbox_path_on_publish", AsyncMock()),
        patch.object(session_service, "dispatch_plugin_event", MagicMock()),
    )


@contextlib.contextmanager
def _supervisor_session(terminal, *extra):
    """Enter the supervisor mock stack plus any test-specific patches."""
    with contextlib.ExitStack() as stack:
        for patcher in (*_supervisor_session_patches(terminal), *extra):
            stack.enter_context(patcher)
        yield


class TestCreateSessionWiring:
    """``create_session`` is the repo-agnostic choke point (#473)."""

    @pytest.mark.asyncio
    async def test_supervisor_session_gets_the_window(self):
        terminal = MagicMock(id="f7020001", session_name="cao-f702")
        ensure = MagicMock(return_value=True)
        with _supervisor_session(
            terminal,
            patch.object(fleet_window_service, "ensure_fleet_window", ensure),
        ):
            result = await create_session(
                provider="kiro_cli",
                agent_profile="chao_supervisor",
                env_vars={"F702_PROBE": "sentinel"},
            )

        assert result is terminal
        # Called with the session actually created and the canonical session
        # env — the channel the --env opt-out travels on.
        ensure.assert_called_once()
        session_name, env = ensure.call_args.args
        assert session_name == "cao-f702"
        # An operator-forwarded var survives verbatim, and the canonical floor
        # is present. Asserting the probe rather than the artifact root keeps
        # this independent of any path remapping the test environment applies.
        assert env["F702_PROBE"] == "sentinel"
        assert "CAO_ARTIFACTS_DIR" in env

    @pytest.mark.asyncio
    async def test_opt_out_env_reaches_the_service_and_creates_nothing(self):
        """``--env CAO_FLEET_TUI=0`` → request env_vars → no window (#473 AC3b)."""
        terminal = MagicMock(id="f7020002", session_name="cao-f702-optout")
        run = MagicMock()
        with _supervisor_session(
            terminal,
            patch.object(fleet_window_service.shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            patch.object(fleet_window_service.subprocess, "run", run),
        ):
            result = await create_session(
                provider="kiro_cli",
                agent_profile="chao_supervisor",
                env_vars={"CAO_FLEET_TUI": "0"},
            )

        assert result is terminal
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_worker_session_gets_no_window(self):
        terminal = MagicMock(id="f7020003", session_name="cao-f702-worker")
        ensure = MagicMock(return_value=True)
        with (
            patch.object(session_service, "create_terminal", AsyncMock(return_value=terminal)),
            patch.object(
                session_service,
                "load_agent_profile",
                MagicMock(return_value=MagicMock(role="worker")),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
                AsyncMock(return_value=None),
            ),
            patch.object(session_service, "dispatch_plugin_event", MagicMock()),
            patch.object(fleet_window_service, "ensure_fleet_window", ensure),
        ):
            await create_session(provider="kiro_cli", agent_profile="developer")

        ensure.assert_not_called()

    @pytest.mark.asyncio
    async def test_absent_fleet_extra_does_not_crash_session_creation(self):
        """The real service runs: no ``cao-fleet`` binary, session still starts."""
        terminal = MagicMock(id="f7020004", session_name="cao-f702-noextra")
        run = MagicMock()
        with _supervisor_session(
            terminal,
            patch.object(fleet_window_service.shutil, "which", return_value=None),
            _fleet_extra(present=False),
            patch.object(fleet_window_service.subprocess, "run", run),
        ):
            result = await create_session(provider="kiro_cli", agent_profile="chao_supervisor")

        assert result is terminal
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_tmux_explosion_does_not_crash_session_creation(self):
        """The real service runs and tmux is missing entirely: session still starts."""
        terminal = MagicMock(id="f7020005", session_name="cao-f702-notmux")
        with _supervisor_session(
            terminal,
            patch.object(fleet_window_service.shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            patch.object(
                fleet_window_service.subprocess,
                "run",
                MagicMock(side_effect=FileNotFoundError("tmux")),
            ),
        ):
            result = await create_session(provider="kiro_cli", agent_profile="chao_supervisor")

        assert result is terminal

    @pytest.mark.asyncio
    async def test_missing_profile_gets_no_window(self):
        """An unknown profile (load raises FileNotFoundError) is not a supervisor."""
        terminal = MagicMock(id="f7020006", session_name="cao-f702-noprofile")
        ensure = MagicMock(return_value=True)
        with (
            patch.object(session_service, "create_terminal", AsyncMock(return_value=terminal)),
            patch.object(
                session_service,
                "load_agent_profile",
                MagicMock(side_effect=FileNotFoundError("no such profile")),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
                AsyncMock(return_value=None),
            ),
            patch.object(session_service, "dispatch_plugin_event", MagicMock()),
            patch.object(fleet_window_service, "ensure_fleet_window", ensure),
        ):
            await create_session(provider="kiro_cli", agent_profile="ghost")

        ensure.assert_not_called()
