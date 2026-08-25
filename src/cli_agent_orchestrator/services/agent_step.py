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
import os
import time
from enum import Enum
from typing import Callable, Optional

from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.kiro_engine import KiroEngine, parse_kiro_engine
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services import receiver_state_view, terminal_service
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.step_fingerprint import StepCallFields, compute
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
    input_gen: int = 0,
    timeout: float,
    polling_interval: float = 1.0,
    cancel_signal: Optional[asyncio.Event] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> _CompletionOutcome:
    """Poll the in-process monitor for a post-input terminal outcome.

    ``cancel_signal`` is the canonical cooperative-cancel name (workflow engine).
    ``cancel_event`` is accepted as an upstream alias for the same Event.
    When the signal fires mid-wait, raises ``StepCancelledError`` promptly
    (issue #409b) so cancel latency is not bounded by the poll interval.
    """
    from cli_agent_orchestrator.services.auto_responder import auto_responder

    if cancel_signal is None:
        cancel_signal = cancel_event
    elif cancel_event is not None and cancel_event is not cancel_signal:
        raise TypeError("pass only one of cancel_signal/cancel_event")

    start = time.time()
    while time.time() - start < timeout:
        if cancel_signal is not None and cancel_signal.is_set():
            raise StepCancelledError(terminal_id=terminal_id)
        current = receiver_state_view.snapshot_view(
            "agent_step.status_reads",
            terminal_id,
            max_age_s=10.0,
            none_behavior="legacy",
            monitor=status_monitor,
        )
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
        if current in (TerminalStatus.COMPLETED, TerminalStatus.IDLE):
            logger.info(
                "run_agent_step: rejected stale %s for terminal %s: status_gen=%s input_gen=%s",
                current.value,
                terminal_id,
                status_gen,
                input_gen,
            )
        # Sleep one poll interval, but wake IMMEDIATELY if cancel fires so the
        # cancel latency is not bounded below by the poll cadence (#409b).
        if cancel_signal is not None:
            try:
                await asyncio.wait_for(cancel_signal.wait(), timeout=polling_interval)
            except asyncio.TimeoutError:
                pass
            else:
                raise StepCancelledError(terminal_id=terminal_id)
        else:
            await asyncio.sleep(polling_interval)
    if cancel_signal is not None and cancel_signal.is_set():
        raise StepCancelledError(terminal_id=terminal_id)
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


async def _validate_reused_terminal(
    terminal_id: str,
    requested_provider: str,
    requested_engine: Optional[KiroEngine | str],
) -> None:
    """Require reuse constraints to agree with authoritative terminal metadata.

    When no engine is requested and metadata is missing, skip (best-effort):
    pre-engine reuse unit tests mock the terminal layer without DB metadata.
    An explicit engine always requires live metadata so KAS/v2 cannot be
    misapplied to a missing or mismatched terminal.
    """
    metadata = await asyncio.to_thread(terminal_service.get_terminal_metadata, terminal_id)
    if metadata is None:
        if requested_engine is None:
            return
        raise ValueError(f"Terminal '{terminal_id}' not found")

    persisted_provider = metadata.get("provider")
    if persisted_provider != requested_provider:
        raise ValueError(
            f"Provider mismatch for reused terminal '{terminal_id}': "
            f"requested {requested_provider!r}, persisted {persisted_provider!r}"
        )

    if requested_engine is None:
        return
    if persisted_provider != ProviderType.KIRO_CLI.value:
        raise ValueError("Kiro engine selection is only valid for provider 'kiro_cli'")

    explicit_engine = parse_kiro_engine(requested_engine)
    assert explicit_engine is not None

    persisted_engine = parse_kiro_engine(metadata.get("engine"))
    if persisted_engine is None:
        # Legacy Kiro rows predate the engine column and are v2 by definition.
        persisted_engine = KiroEngine.V2
    if explicit_engine != persisted_engine:
        raise ValueError(
            f"Kiro engine mismatch for reused terminal '{terminal_id}': "
            f"requested {explicit_engine.value!r}, persisted {persisted_engine.value!r}"
        )


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


class StepCancelledError(StepExecutionError):
    """The in-flight step wait was interrupted by a cancellation signal (#409b).

    Distinct from a run-failure: a cancellation is NOT retried. Subclasses
    ``StepExecutionError`` with ``kind="cancelled"`` so callers that only know
    the structured-kind contract (workflow engine) and callers that catch the
    dedicated type (upstream #409b) both observe the same event.
    """

    def __init__(self, terminal_id: Optional[str] = None) -> None:
        super().__init__(
            "step wait interrupted by cancellation",
            kind="cancelled",
            terminal_id=terminal_id,
        )


def _resolve_cancel_signal(
    cancel_signal: Optional[asyncio.Event],
    cancel_event: Optional[asyncio.Event],
) -> Optional[asyncio.Event]:
    """Canonicalize cancel_signal; accept upstream cancel_event as alias."""
    if cancel_signal is None:
        return cancel_event
    if cancel_event is not None and cancel_event is not cancel_signal:
        raise TypeError("pass only one of cancel_signal/cancel_event")
    return cancel_signal


async def resolve_effective_working_directory(
    working_directory: Optional[str],
    caller_id: Optional[str],
) -> Optional[str]:
    """Resolve the directory a freshly created terminal will ACTUALLY run in.

    Extracted verbatim from ``run_agent_step``'s create path (issue #583, unit
    ``run-step-replay-branch`` BR-10/TD-1) because two callers now need the same
    answer and there must be exactly ONE computation of it:

    * ``run_agent_step`` itself, which forwards the result to
      ``terminal_service.create_terminal`` and hashes it as
      ``StepCallFields.effective_working_directory``;
    * the ``POST /terminals/run-step`` route, which must compute a script step's
      call fingerprint BEFORE it decides whether to execute at all — and
      ``step-fingerprint``'s BR-5 permits only the EFFECTIVE directory in that
      hash. Hashing the POSTED value would not match what ``begin_step`` stored,
      so every ``caller_id``-inherited step would read as a false ``DIVERGED``.

    The route passes its answer back in through ``run_agent_step``'s existing
    ``working_directory`` parameter, so the call below simply returns it
    unchanged and no resolution happens twice. There is deliberately NO
    ``skip_resolution`` flag: a parameter whose only purpose is to disable a
    branch is the inert-parameter shape this issue has removed three times.

    BEST-EFFORT, AND THAT IS THE CONTRACT (unchanged by the extraction).
    ``asyncio.CancelledError`` is re-raised so a cancelled step stays cancelled;
    any other failure is logged and the caller falls back to the server default,
    because CWD inheritance must never fail a step that could otherwise run.

    ``caller_id`` is not authenticated/authorized (it arrives via an HTTP body);
    this is consistent with its existing use for callback routing (#284). The
    resolved path still passes ``_resolve_and_validate_working_directory`` inside
    ``create_terminal``, so risk is confined to inheriting a real existing pane's
    CWD in a single-user trust model.

    Args:
        working_directory: the explicitly requested directory, or None.
        caller_id: the supervisor terminal whose pane CWD is inherited when
            ``working_directory`` is None.

    Returns:
        ``working_directory`` when it was supplied or there is no caller to
        inherit from; the caller terminal's CWD when resolution succeeds and
        returns a non-empty path; otherwise ``working_directory`` unchanged
        (i.e. None — the server default).
    """
    # The guard is the extracted block's own condition, inverted into an early
    # return. An explicit directory always wins, and with no caller_id there is
    # nothing to inherit from.
    if working_directory is not None or caller_id is None:
        return working_directory
    try:
        resolved = await asyncio.to_thread(terminal_service.get_working_directory, caller_id)
        if resolved:
            return resolved
    except asyncio.CancelledError:
        raise
    except (
        Exception
    ) as exc:  # noqa: BLE001 — CWD inheritance is best-effort; step must not fail on it
        logger.warning(
            "resolve_effective_working_directory: failed to resolve working directory "
            "from caller %r, falling back to server default: %r",
            caller_id,
            exc,
        )
    return working_directory


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
    on_step_terminal_ready: Optional[Callable[[str, str], None]] = None,
    cancel_signal: Optional[asyncio.Event] = None,
    cancel_event: Optional[asyncio.Event] = None,
    engine: Optional[KiroEngine | str] = None,
    model: Optional[str] = None,
    use_worktree: Optional[bool] = None,
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
            terminal (ignored when reusing a terminal). When None and
            ``caller_id`` is set, the worker inherits the caller's pane CWD
            via ``get_working_directory()`` (best-effort; falls back to the
            server default on failure).
        caller_id: Terminal ID of the supervisor creating this terminal, recorded
            so ``send_message`` can route callbacks structurally (issue #284).
            Also used to inherit the working directory when
            ``working_directory`` is None (best-effort). None for
            operator-launched / engine steps with no supervisor.
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
        on_step_terminal_ready: Optional callback invoked with
            ``(terminal_id, call_fingerprint)`` as soon as the terminal this step
            will run on EXISTS and before the prompt is sent. It fires on BOTH
            paths (issue #583, unit ``settlement-rewire`` BR-3): on the
            create path immediately after ``terminal_service.create_terminal``
            returns and BEFORE the readiness wait; on the reuse path immediately
            after the reused terminal is validated. Firing on the reuse path too
            is what gives every script step a durable ``running`` row before it
            executes — without it, a terminal-reuse call would have none and
            FR-4's guard would cover only steps that made their own terminal.
            THIS PARAMETER WAS RENAMED BECAUSE FIRING IT ON THE REUSE PATH MADE
            ITS FORMER NAME — which spoke only of terminal creation — FALSE
            (BR-4). The former name is deliberately not spelled here: a test
            greps the whole of ``src/`` for it, so the one place it survives must
            be the changelog, not the code.
            Two consumers today, both in ``script_runner``: U4's orphan sweep
            (BR-31) records the live terminal into the shared ``ScriptRunRecord``
            ``step_states`` map, so a subprocess that crashes/times out while a
            run-step call is mid-flight still leaves the in-flight terminal
            visible to ``_reconcile_orphans``; and the journal's ``begin_step``
            writes the durable ``running`` row carrying ``call_fingerprint``. A
            callback exception is logged and swallowed — step bookkeeping must
            never fail a live step. Default None = behavior unchanged.
        cancel_signal: Optional same-loop cooperative cancellation signal. It is
            checked before send, during the completion poll, and before extraction.
            Synchronous send/extraction already running in ``to_thread`` cannot be
            force-cancelled; cancellation is classified when that call returns.
            ``cancel_event`` is accepted as an upstream alias for the same Event.
        model: Explicit per-call model override for a freshly created
            terminal (ignored when reusing a terminal), forwarded to
            ``terminal_service.create_terminal``. Lets a handoff caller pin
            a specific model for this one worker without a dedicated agent
            profile. Default None preserves the provider's existing profile
            and providers.toml resolution unchanged.
        use_worktree: Issue #100 Phase 1. When True and a terminal is created
            here (``reuse_terminal_id`` is None), the freshly created terminal
            gets an isolated ``git worktree`` instead of sharing
            ``working_directory`` as given — see
            ``terminal_service.create_terminal``'s own docstring for the
            resolution/teardown mechanics. Ignored when reusing a terminal.
            Default False = behavior unchanged.

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
        # Inherit working directory from supervisor when not explicitly set.
        # Without this, a handoff worker starts in the cao-server process CWD
        # instead of the supervisor's project directory. Best-effort: if
        # resolution fails, fall back to the server default.
        #
        # THE COMPUTATION LIVES IN ``resolve_effective_working_directory`` (issue
        # #583, unit ``run-step-replay-branch`` BR-10/TD-1) because the run-step
        # route must know this answer BEFORE it calls this function — it needs the
        # effective directory to compute the call fingerprint the replay gate
        # compares. When the route has already resolved, it passes the result in
        # as ``working_directory`` and the helper returns it unchanged, so the
        # resolution never runs twice and no flag is needed. Duplicating the
        # computation instead would be the "two implementations of one
        # security-relevant value" defect FR-2 exists to prevent.
        working_directory = await resolve_effective_working_directory(working_directory, caller_id)

    # The step's ``v2`` call identity (issue #583, unit ``settlement-rewire`` BR-1), computed
    # in the ONE window ``step-fingerprint``'s BR-5 permits: AFTER the working-directory
    # resolution above and BEFORE terminal creation below.
    #
    # THE WINDOW IS THE WHOLE REASON THIS LIVES HERE rather than in either callback. Both
    # callback factories are built in the route (``api/main.py``) before ``run_agent_step`` is
    # called at all — hence before resolution — and the settle callback runs later still, once
    # the step has already executed. ``effective_working_directory`` must be the directory the
    # step ACTUALLY ran in: when ``working_directory is None and caller_id is not None`` the
    # block above replaces it with the caller terminal's CWD, so hashing the POSTED value
    # would give two runs that executed in genuinely different directories one identity, and
    # one would replay the other's result.
    #
    # ONE STATEMENT, UNCONDITIONAL — computed exactly once per step (INV-1). The
    # ``if created_here:`` test is repeated below rather than folding this into either branch,
    # because a per-branch computation would duplicate the field assembly and the two copies
    # could drift.
    #
    # On the reuse path the four creation-only components are sentinel-ised by ``compute``
    # itself (BR-1a/BR-5), which is CORRECT and must not be "fixed": the resolution block
    # above is inside ``if created_here:``, so on a reuse call those fields describe a
    # terminal this call did not make and the implementation discards them. The tuple is never
    # shortened — ten components on both paths.
    #
    # The digest is NEVER logged, echoed or put in an exception (SR-7).
    call_fingerprint = compute(
        StepCallFields(
            provider=provider,
            agent=agent,
            prompt=prompt,
            model=model,
            # ``StepCallFields.engine`` is the enum's ``value`` by contract — the CALLER
            # normalises, so ``step_fingerprint`` can stay a stdlib-only leaf module.
            engine=engine.value if isinstance(engine, KiroEngine) else engine,
            allowed_tools=None if allowed_tools is None else tuple(allowed_tools),
            effective_working_directory=working_directory,
            use_worktree=use_worktree,
            reused_terminal=not created_here,
            timeout=timeout,
        )
    )

    def _notify_terminal_ready(ready_terminal_id: str) -> None:
        """Fire ``on_step_terminal_ready`` best-effort — bookkeeping never fails a step.

        Called from BOTH paths (BR-3). Kept as one nested helper with one ``try`` so the
        two call sites cannot diverge in their error posture, while each keeps its own
        position guarantee: on the create path this must run BEFORE the readiness wait
        (BR-31's window), which is why the invocation is not simply hoisted below the
        create/reuse branch.
        """
        if on_step_terminal_ready is None:
            return
        try:
            on_step_terminal_ready(ready_terminal_id, call_fingerprint)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — step bookkeeping is best-effort; step must not fail on it
            logger.warning(
                "run_agent_step: on_step_terminal_ready callback failed for terminal %s: %s",
                ready_terminal_id,
                exc,
            )

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
        fork_context = await terminal_service.seed_resume_bootstrap(
            agent, provider, working_directory or os.getcwd()
        )
        terminal = await terminal_service.create_terminal(
            provider,
            agent,
            session_name=session_name,
            new_session=new_session,
            working_directory=working_directory,
            allowed_tools=allowed_tools,
            caller_id=caller_id,
            env_vars=env_vars,
            fork_context=fork_context,
            engine=engine,
            model=model,
            use_worktree=use_worktree,
        )
        terminal_id = terminal.id

        # BR-31: make the terminal this call just made visible to U4's orphan
        # sweep, and (issue #583, BR-3) write its durable ``running`` row, BEFORE
        # the readiness wait / input send — the dangerous edge is a subprocess
        # that dies while this call is mid-flight, between the terminal
        # appearing and the journal write. Doing both now closes that window,
        # and the position matters: the readiness wait below can run for
        # ``ready_timeout`` seconds, so notifying after it would reopen exactly
        # the gap BR-31 was added to close.
        _notify_terminal_ready(terminal_id)

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
    else:
        assert terminal_id is not None
        await _validate_reused_terminal(terminal_id, provider, engine)
        # BR-3: the reuse path notifies too. Until this unit the hook fired only
        # inside the create branch, so a terminal-reuse call wrote NO durable
        # ``running`` row and FR-4's guard covered only steps that made their own
        # terminal, leaving reuse to depend on the journal's no-begin rescue
        # instead. Notifying after validation rather than before it keeps the
        # order honest: a call rejected by ``_validate_reused_terminal`` never
        # ran, so it must not leave a ``running`` row behind.
        _notify_terminal_ready(terminal_id)

    assert terminal_id is not None  # for type-checkers: set in both branches
    cancel_signal = _resolve_cancel_signal(cancel_signal, cancel_event)
    cleanup = False
    extraction_succeeded = False
    try:
        if cancel_signal is not None and cancel_signal.is_set():
            cleanup = True
            raise StepCancelledError(terminal_id=terminal_id)

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
                raise StepCancelledError(terminal_id=terminal_id) from exc
            current = receiver_state_view.snapshot_view(
                "agent_step.status_reads",
                terminal_id,
                max_age_s=10.0,
                none_behavior="legacy",
                monitor=status_monitor,
            )
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
                raise StepCancelledError(terminal_id=terminal_id) from exc
            raise

        if cancel_signal is not None and cancel_signal.is_set():
            cleanup = True
            raise StepCancelledError(terminal_id=terminal_id)

        input_gen = status_monitor.get_input_gen(terminal_id)
        try:
            outcome = await _wait_for_completion(
                terminal_id,
                input_gen=input_gen,
                timeout=timeout,
                cancel_signal=cancel_signal,
            )
        except StepCancelledError:
            cleanup = True
            raise
        if outcome == _CompletionOutcome.CANCELLED:
            cleanup = True
            raise StepCancelledError(terminal_id=terminal_id)
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
            current = receiver_state_view.snapshot_view(
                "agent_step.status_reads",
                terminal_id,
                max_age_s=10.0,
                none_behavior="legacy",
                monitor=status_monitor,
            )
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

        final_status = receiver_state_view.snapshot_view(
            "agent_step.status_reads",
            terminal_id,
            max_age_s=10.0,
            none_behavior="legacy",
            monitor=status_monitor,
        )
        if final_status == TerminalStatus.ERROR:
            raise StepExecutionError(
                f"terminal {terminal_id} reached ERROR status",
                kind="error",
                terminal_id=terminal_id,
            )
        if cancel_signal is not None and cancel_signal.is_set():
            cleanup = True
            raise StepCancelledError(terminal_id=terminal_id)

        try:
            last_message = await asyncio.to_thread(
                terminal_service.get_output, terminal_id, OutputMode.LAST
            )
            extraction_succeeded = True
        except Exception as exc:
            if cancel_signal is not None and cancel_signal.is_set():
                cleanup = True
                raise StepCancelledError(terminal_id=terminal_id) from exc
            cleanup = True
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
