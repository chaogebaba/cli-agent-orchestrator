"""Shared agent-step execution substrate (issue #312, unit N0).

``run_agent_step`` is the single canonical create -> input -> wait -> extract ->
teardown sequence for driving one agent through one step. It is the shared
substrate both step callers converge on, SERVER-SIDE:

- the run engine (N5, future) calls it directly IN-PROCESS;
- the handoff MCP client reaches it over the single combined HTTP endpoint
  ``POST /terminals/run-step`` (api/main.py), replacing its former six granular
  round-trips.

It depends ONLY on the terminal layer (``terminal_service`` + the provider
manager), so it is backend-agnostic (BR-10/RD-4): correctness holds on the tmux
backend alone, with no per-step tmux/herdr branching.

Failure contract (RD-2.1 / REL-3.3): ``run_agent_step`` returns an
``AgentStepResult`` ONLY on success (status COMPLETED). Every failure mode —
the readiness/completion wait timing out, the terminal reaching
``TerminalStatus.ERROR`` — RAISES a narrow exception. It NEVER returns a falsy
or ``None`` "success". The caller (engine) maps the raised exception to its 3x
retry policy (FR-5.3); the HTTP handler maps it to an ``HTTPException``.
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Callable, Optional

from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.terminal_service import OutputMode, TerminalInputBlockedError
from cli_agent_orchestrator.utils.terminal import wait_until_status

logger = logging.getLogger(__name__)

# Ready states a freshly created terminal may settle into before it can accept
# input (mirrors the handoff readiness wait): some providers process their
# system prompt as the first turn and reach COMPLETED without a bare IDLE.
_READY_STATES = {TerminalStatus.IDLE, TerminalStatus.COMPLETED}

# Generous readiness timeout: provider init (shell warm-up + CLI startup + MCP
# registration + auth) can take ~15-45s. Matches the handoff caller's 120s.
DEFAULT_READY_TIMEOUT = 120.0


class _CompletionOutcome(str, Enum):
    COMPLETED = "completed"
    IDLE_DONE = "idle_done"
    ERROR = "error"
    WAITING_USER = "waiting_user"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"  # Reserved for FX14b.


async def _wait_for_completion(
    terminal_id: str,
    *,
    input_gen: int,
    timeout: float,
    polling_interval: float = 1.0,
    cancel_signal: Optional[asyncio.Event] = None,
) -> _CompletionOutcome:
    """Poll the in-process monitor for a post-input terminal outcome."""
    from cli_agent_orchestrator.services.auto_responder import auto_responder

    start = time.time()
    while time.time() - start < timeout:
        if cancel_signal is not None and cancel_signal.is_set():
            return _CompletionOutcome.CANCELLED
        current = status_monitor.get_status(terminal_id)
        if current == TerminalStatus.ERROR:
            return _CompletionOutcome.ERROR
        if current == TerminalStatus.WAITING_USER_ANSWER:
            if auto_responder.waiting_gate(terminal_id):
                return _CompletionOutcome.WAITING_USER
        elif current == TerminalStatus.COMPLETED:
            status_gen = status_monitor.get_status_gen(terminal_id)
            if status_gen is None or status_gen >= input_gen:
                return _CompletionOutcome.COMPLETED
        elif current == TerminalStatus.IDLE:
            status_gen = status_monitor.get_status_gen(terminal_id)
            if status_gen is not None and status_gen >= input_gen:
                return _CompletionOutcome.IDLE_DONE
        await asyncio.sleep(polling_interval)
    if cancel_signal is not None and cancel_signal.is_set():
        return _CompletionOutcome.CANCELLED
    return _CompletionOutcome.TIMEOUT


async def _teardown_terminal(terminal_id: str, registry: Optional[PluginRegistry]) -> None:
    """Best-effort exit-then-delete for a terminal owned by this step."""
    try:
        await asyncio.to_thread(terminal_service.exit_terminal_cli, terminal_id)
    except Exception as exc:  # noqa: BLE001 - teardown is best-effort
        logger.warning(
            "run_agent_step: failed to send graceful exit to terminal %s before teardown: %s",
            terminal_id,
            exc,
        )
    try:
        await asyncio.to_thread(terminal_service.delete_terminal, terminal_id, registry=registry)
    except Exception as exc:  # noqa: BLE001 - teardown is best-effort
        logger.warning("run_agent_step: failed to tear down terminal %s: %s", terminal_id, exc)


class StepExecutionError(Exception):
    """A step failed to complete successfully.

    Raised for a readiness/completion timeout or a terminal that reached
    ``TerminalStatus.ERROR``. Narrow by design so the caller (engine) can map
    it to its retry policy and the API boundary can map it to an HTTPException.

    Carries two structured fields so callers never have to scrape the message:

    - ``kind`` distinguishes a worker that *ran long* (``"timeout"``), one
      that *crashed* (``"error"``, i.e. the terminal reached ERROR), and one
      blocked on manual input (``"waiting_user_input"``).
    - ``terminal_id`` is the live terminal the step ran on (when known), so a
      failed caller can report/clean it up without regex-scraping the message.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "timeout",
        terminal_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.terminal_id = terminal_id


async def run_agent_step(
    provider: str,
    agent: str,
    prompt: str,
    session_name: Optional[str] = None,
    reuse_terminal_id: Optional[str] = None,
    teardown: bool = True,
    timeout: float = 600.0,
    ready_timeout: float = DEFAULT_READY_TIMEOUT,
    working_directory: Optional[str] = None,
    caller_id: Optional[str] = None,
    allowed_tools: Optional[list[str]] = None,
    registry: Optional[PluginRegistry] = None,
    env_vars: Optional[dict[str, str]] = None,
    on_terminal_created: Optional[Callable[[str], None]] = None,
    cancel_signal: Optional[asyncio.Event] = None,
) -> AgentStepResult:
    """Run one agent step and return its result (success only).

    Sequence:
      1. Create a terminal (or reuse ``reuse_terminal_id``).
      2. Wait until it is ready to accept input (IDLE/COMPLETED).
      3. Send ``prompt`` (sync, bracketed-paste — the existing input path).
      4. Wait until the post-input turn settles (COMPLETED or generation-fresh IDLE).
      5. Extract the last agent message (provider-specific extraction).
      6. Tear the terminal down unless ``teardown=False`` or it was reused.

    Args:
        provider: Provider type string (e.g. "kiro_cli", "claude_code").
        agent: Agent profile name.
        prompt: The message to send. Any caller-side prompt shaping (e.g. the
            codex handoff banner) is applied BEFORE calling this; the substrate
            sends ``prompt`` verbatim.
        session_name: Optional existing session to create the terminal in. When
            provided, the terminal is added as a window to that EXISTING session
            (``new_session=False``). When None, a brand-new tmux session is
            created for this step (``new_session=True``) — auto-naming the
            session inside ``create_terminal``. (Passing None with the implicit
            ``new_session=False`` would always fail: the auto-generated session
            does not yet exist.)
        reuse_terminal_id: Reuse an existing terminal instead of creating one.
            When set, the create + teardown steps are skipped (no pool; the
            caller owns the terminal's lifecycle).
        teardown: When True (default) and the terminal was created here, delete
            it after extraction. Ignored when ``reuse_terminal_id`` is set.
        timeout: Max seconds to wait for the step to settle after input.
        ready_timeout: Max seconds to wait for a freshly created terminal to be
            ready to accept input.
        working_directory: Optional working directory for a freshly created
            terminal (ignored when reusing a terminal).
        caller_id: Terminal ID of the supervisor creating this terminal, recorded
            so send_message can route callbacks structurally (issue #284). None
            for operator-launched / engine steps with no supervisor.
        allowed_tools: Resolved allowed-tools list for the freshly created
            terminal (handoff inheritance). None lets ``create_terminal`` derive
            them from the agent profile.
        registry: Plugin registry forwarded to ``delete_terminal`` on teardown so
            ``post_kill_terminal`` plugin hooks fire (parity with the DELETE
            endpoint). None (the in-process engine path today) means no hooks
            dispatch — behavior unchanged.
        env_vars: Optional per-step environment variables to inject into a newly
            created terminal (ignored when reusing a terminal). The run engine (N5)
            uses this to set ``CAO_WORKFLOW_RUN_ID`` / ``CAO_WORKFLOW_STEP_ID`` so
            the worker's ``workflow_return`` tool routes its structured output to
            the correct ``(run_id, step_id)`` store key. With ``session_name=None``
            they initialize the fresh session. With an existing ``session_name``,
            they overlay that session's shared environment for this window only,
            with per-step values winning on collision and without persistence.
            Default None preserves the session environment unchanged.
        on_terminal_created: Optional callback invoked with the ``terminal_id``
            IMMEDIATELY after a freshly created terminal exists (before the
            readiness wait / input). U4's script-tier orphan sweep (BR-31) uses
            this to record the live terminal into the shared ``ScriptRunRecord``
            ``step_states`` map AT terminal-creation time — so a subprocess that
            crashes/times out while a run-step call is mid-flight still leaves the
            in-flight terminal visible to ``_reconcile_orphans``. Not called for a
            reused terminal (the caller already owns it). A callback exception is
            logged and swallowed — recording a terminal for the sweep must never
            fail the step. Default None = behavior unchanged.
        cancel_signal: Optional same-loop cooperative cancellation signal. It is
            checked before send, during the completion poll, and before extraction.
            Synchronous send/extraction already running in ``to_thread`` cannot be
            force-cancelled; cancellation is classified when that call returns.

    Returns:
        ``AgentStepResult`` with status COMPLETED — ONLY on success.

    Raises:
        StepExecutionError: readiness/completion wait timed out (``kind="timeout"``),
            the terminal reached ``TerminalStatus.ERROR`` (``kind="error"``), or
            a dialog requires manual input (``kind="waiting_user_input"``).
            ``terminal_id`` carries the live terminal so the caller can clean up.
        ValueError / TimeoutError: propagated from ``terminal_service`` (e.g.
            terminal-create failure, unknown terminal) — surfaced, never swallowed.
    """
    created_here = reuse_terminal_id is None
    terminal_id = reuse_terminal_id

    if created_here:
        # When no session_name is supplied we must CREATE a fresh tmux session
        # (new_session=True): create_terminal auto-names it. Leaving the default
        # new_session=False here would auto-generate a name and then immediately
        # fail with "Session '<name>' not found", since that session does not
        # exist yet. When a session_name IS supplied, add a window to it
        # (new_session=False) — this is the handoff "same session as supervisor"
        # path.
        new_session = session_name is None

        # create_terminal already runs provider.initialize() (which waits for
        # IDLE); a failure raises (ValueError/TimeoutError) and propagates.
        terminal = await terminal_service.create_terminal(
            provider,
            agent,
            session_name=session_name,
            new_session=new_session,
            working_directory=working_directory,
            allowed_tools=allowed_tools,
            caller_id=caller_id,
            env_vars=env_vars,
        )
        terminal_id = terminal.id

        # BR-31: make the just-created terminal visible to U4's orphan sweep
        # BEFORE the readiness wait / input send — the dangerous edge is a
        # subprocess that dies while this call is mid-flight, between create and
        # the journal write. Recording it now (into the shared record's
        # step_states) closes that window. Best-effort: a callback failure must
        # never turn a live step into a failure.
        if on_terminal_created is not None:
            try:
                on_terminal_created(terminal_id)
            except (
                Exception
            ) as exc:  # noqa: BLE001 — sweep bookkeeping is best-effort; step must not fail on it
                logger.warning(
                    "run_agent_step: on_terminal_created callback failed for terminal %s: %s",
                    terminal_id,
                    exc,
                )

        # Secondary in-process readiness wait: provider.initialize() can return a
        # false-positive on the shell prompt before the CLI is truly ready, so we
        # confirm a ready status before sending input (same guard handoff uses).
        ready = await wait_until_status(terminal_id, _READY_STATES, timeout=ready_timeout)
        if not ready:
            # Surface the live terminal so it can be inspected/cleaned up, then
            # fail fast. We do NOT auto-delete here: leaving the terminal lets
            # the caller decide (handoff surfaces terminal_id on failure).
            raise StepExecutionError(
                f"terminal {terminal_id} did not reach a ready status within " f"{ready_timeout}s",
                kind="timeout",
                terminal_id=terminal_id,
            )

    assert terminal_id is not None  # for type-checkers: set in both branches
    cleanup = False
    extraction_succeeded = False
    try:
        if cancel_signal is not None and cancel_signal.is_set():
            cleanup = True
            raise StepExecutionError(
                f"step on terminal {terminal_id} was cancelled",
                kind="cancelled",
                terminal_id=terminal_id,
            )

        try:
            await asyncio.to_thread(
                terminal_service.send_input,
                terminal_id,
                prompt,
                orchestration_type=OrchestrationType.HANDOFF,
            )
        except TerminalInputBlockedError as exc:
            if cancel_signal is not None and cancel_signal.is_set():
                cleanup = True
                raise StepExecutionError(
                    f"step on terminal {terminal_id} was cancelled during send",
                    kind="cancelled",
                    terminal_id=terminal_id,
                ) from exc
            current = status_monitor.get_status(terminal_id)
            status_value = current.value if hasattr(current, "value") else str(current)
            raise StepExecutionError(
                f"terminal {terminal_id} is waiting on a dialog "
                f"(status={status_value}); input blocked",
                kind="input_blocked",
                terminal_id=terminal_id,
            ) from exc
        except DeliveryDeferredError as exc:
            raise StepExecutionError(
                str(exc),
                kind="delivery_deferred",
                terminal_id=terminal_id,
            ) from exc
        except Exception as exc:
            if cancel_signal is not None and cancel_signal.is_set():
                cleanup = True
                raise StepExecutionError(
                    f"step on terminal {terminal_id} was cancelled during send",
                    kind="cancelled",
                    terminal_id=terminal_id,
                ) from exc
            raise

        if cancel_signal is not None and cancel_signal.is_set():
            cleanup = True
            raise StepExecutionError(
                f"step on terminal {terminal_id} was cancelled",
                kind="cancelled",
                terminal_id=terminal_id,
            )

        input_gen = status_monitor.get_input_gen(terminal_id)
        outcome = await _wait_for_completion(
            terminal_id,
            input_gen=input_gen,
            timeout=timeout,
            cancel_signal=cancel_signal,
        )
        if outcome == _CompletionOutcome.CANCELLED:
            cleanup = True
            raise StepExecutionError(
                f"step on terminal {terminal_id} was cancelled",
                kind="cancelled",
                terminal_id=terminal_id,
            )
        if outcome == _CompletionOutcome.ERROR:
            raise StepExecutionError(
                f"terminal {terminal_id} reached ERROR status",
                kind="error",
                terminal_id=terminal_id,
            )
        if outcome == _CompletionOutcome.WAITING_USER:
            raise StepExecutionError(
                f"terminal {terminal_id} is waiting for user input",
                kind="waiting_user_input",
                terminal_id=terminal_id,
            )
        if outcome == _CompletionOutcome.TIMEOUT:
            current = status_monitor.get_status(terminal_id)
            if current == TerminalStatus.ERROR:
                raise StepExecutionError(
                    f"terminal {terminal_id} reached ERROR status",
                    kind="error",
                    terminal_id=terminal_id,
                )
            raise StepExecutionError(
                f"step on terminal {terminal_id} did not complete within {timeout}s",
                kind="timeout",
                terminal_id=terminal_id,
            )

        final_status = status_monitor.get_status(terminal_id)
        if final_status == TerminalStatus.ERROR:
            raise StepExecutionError(
                f"terminal {terminal_id} reached ERROR status",
                kind="error",
                terminal_id=terminal_id,
            )
        if cancel_signal is not None and cancel_signal.is_set():
            cleanup = True
            raise StepExecutionError(
                f"step on terminal {terminal_id} was cancelled before extraction",
                kind="cancelled",
                terminal_id=terminal_id,
            )

        try:
            last_message = await asyncio.to_thread(
                terminal_service.get_output, terminal_id, OutputMode.LAST
            )
            extraction_succeeded = True
        except Exception as exc:
            if cancel_signal is not None and cancel_signal.is_set():
                cleanup = True
                raise StepExecutionError(
                    f"step on terminal {terminal_id} was cancelled during extraction",
                    kind="cancelled",
                    terminal_id=terminal_id,
                ) from exc
            raise

        cleanup = True
        return AgentStepResult(
            terminal_id=terminal_id,
            last_message=last_message,
            status=TerminalStatus.COMPLETED,
        )
    finally:
        # Extraction success is the success boundary: a concurrent cancel is
        # handled by the workflow at the next step boundary, while this step
        # remains successful. All other cleanup classifications were set above.
        if extraction_succeeded:
            cleanup = True
        if cleanup and teardown and created_here:
            await _teardown_terminal(terminal_id, registry)
