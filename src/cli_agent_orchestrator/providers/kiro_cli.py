"""Kiro CLI provider implementation.

This module provides the KiroCliProvider class for integrating with Kiro CLI,
an AI-powered coding assistant that operates through a terminal interface.

Kiro CLI Features:
- Agent-based conversations with customizable profiles
- File system access and code manipulation capabilities
- Interactive permission prompts for sensitive operations
- ANSI-colored output with distinctive prompt patterns

The provider detects the following terminal states:
- IDLE: Agent is waiting for user input (shows agent prompt)
- PROCESSING: Agent is generating a response
- COMPLETED: Agent has finished responding (shows green arrow + response)
- WAITING_USER_ANSWER: Agent is waiting for permission confirmation
- ERROR: Agent encountered an error during processing
"""

import asyncio
import json
import logging
import re
import shlex
import time
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import BLOCKED_WAIT_CAP_S, KIRO_AGENTS_DIR
from cli_agent_orchestrator.models.kiro_engine import KiroEngine, resolve_kiro_engine
from cli_agent_orchestrator.models.terminal import ForkContext, TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.kiro_capabilities import build_kiro_command
from cli_agent_orchestrator.services.settings_service import (
    get_provider_defaults,
    get_provider_profile_defaults,
    get_server_settings,
    resolve_provider_string_option,
)
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import (
    BlockedWaitPolicy,
    wait_for_shell,
    wait_until_status,
)
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# =============================================================================
# Regex Patterns for Kiro CLI Output Analysis
# =============================================================================

# Green arrow pattern indicates the start of an agent response (escape-stripped)
# Example: "> Here is the code you requested..."
GREEN_ARROW_PATTERN = r"^>\s*"

# SGR (colour) escape codes only. Used by get_status, which strips colour but
# MUST preserve carriage returns and cursor-movement sequences: the permission
# detection counts idle prompts per newline-delimited line, and Kiro renders
# active prompts with \r in-place redraws (same line, no \n). strip_terminal_
# escapes would normalise \r -> \n and split those redraws onto separate lines,
# making an active permission prompt look idle (inbox would then deliver during
# a permission prompt — see test_permission_prompt_detection).
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Additional escape sequences that may appear in terminal output
ESCAPE_SEQUENCE_PATTERN = r"\[[?0-9;]*[a-zA-Z]"

# Control characters to strip from final output
CONTROL_CHAR_PATTERN = r"[\x00-\x1f\x7f-\x9f]"

# Legacy-UI IDLE prompt pattern for log files (with ANSI codes)
IDLE_PROMPT_PATTERN_LOG = r"\x1b\[38;5;\d+m\[.+?\].*\x1b\[38;5;\d+m>\s*\x1b\[\d*m"

# =============================================================================
# New TUI Patterns (Kiro CLI without --legacy-ui)
# =============================================================================

# New TUI idle prompt: "Ask a question or describe a task ↵"
# Case-insensitive match; comma between "question" and "or" is optional
# (older versions used lowercase with comma, v1.29+ uses capitalized without)
NEW_TUI_IDLE_PATTERN = r"[Aa]sk a question,? or describe a task"

# New TUI IDLE prompt pattern for log files (with ANSI codes)
NEW_TUI_IDLE_PATTERN_LOG = r"[Aa]sk a question,? or describe a task"

# TUI separator line: horizontal bar (────) used to delimit sections.
# Require 20+ chars to avoid matching short markdown separators in agent output.
TUI_SEPARATOR_PATTERN = r"^[─]{20,}$"

# Non-SGR CSI sequences (erase-line \x1b[2K, cursor moves, etc.) — everything
# except colour codes ending in 'm'. Compiled once at module scope because
# get_status() is on a hot path (called per output burst); recompiling per call
# was avoidable overhead. Used in Check 6 to strip TUI chrome so the separator
# regex can anchor on the raw ─── characters.
_NON_SGR_CSI = re.compile(r"\x1b\[[0-9;?]*[^m]")

# TUI Credits line: "▸ Credits: N.NN • Time: Ns" marks response completion
TUI_CREDITS_PATTERN = r"▸\s*Credits:\s*[\d.]+"

# TUI processing indicator: ghost text shown while agent is working.
# kiro-cli 2.11+ replaced "Kiro is working" with "Thinking..." (with an
# optional "(esc to cancel)" suffix). Match either variant.
TUI_PROCESSING_PATTERN = r"Kiro is working|Thinking\.\.\."

# TUI initialization indicator: shown during startup before chat is ready.
# Kiro TUI renders the idle prompt placeholder ("Ask a question or describe
# a task") *before* the "● Initializing..." phase completes, which caused a
# premature IDLE verdict. "Initializing..." is cleared by Kiro once startup
# finishes, so its presence unconditionally means PROCESSING (unlike the
# "Kiro is working" ghost text, which can linger as stale after a redraw).
#
# Also covers the MCP-server boot line "M of N mcp servers initialized.
# ctrl-c to start chatting now" — Kiro shows this *before* the idle prompt
# is interactive, so a paste sent during this window is absorbed by the
# pre-prompt boot screen and silently dropped (observed during e2e
# allowed-tools tests).
#
# kiro-cli 2.8.x also shows "Initializing · type to queue a message" during
# boot (different from the "Initializing..." with three dots).
TUI_INITIALIZING_PATTERN = (
    r"Initializing\.\.\."
    r"|\d+ of \d+ mcp servers initialized\.\s*ctrl-c to start chatting now"
    r"|Initializing\s*·\s*type to queue a message"
)


# F581 D16: busy-marker veto hook for rule 3a (same contract as claude_code's
# ``rule3a_busy_marker``). The kiro TUI shows two provable "the agent's own turn
# is live" markers — the footer ghost text (``Kiro is working`` / ``Thinking...``)
# and the spinner status line (``◐ N tasks remaining · …``, animated braille/moon
# glyph). These are proven from the byte-exact captured panes under
# ``test/providers/fixtures/busy_marker/kiro_cli/``. Anchored so agent output that
# merely quotes the phrase cannot flip the verdict.
KIRO_BUSY_MARKER_PATTERN = re.compile(
    r"Kiro is working|Thinking\.\.\.|[◐◓◑◒⣾⣽⣻⢿⡿⣟⣯⣷]\s+\d+\s+task(?:s)? remaining"
)


def kiro_busy_marker_live(text: str) -> bool | None:
    """F581 D16: is the kiro seat's own TUI activity marker live in ``text``?

    Truth table (same as D12d / claude_code):
      * ``True``  — a busy marker (``Kiro is working`` / ``Thinking...`` / the
        ``◐ N tasks remaining`` spinner status line) is present.
      * ``False`` — no busy marker BUT the idle prompt placeholder is present
        (the seat's own turn is over): veto this sample's upgrade.
      * ``None``  — no identifiable kiro TUI (no marker, no idle prompt): legacy
        rule 3a applies unchanged.

    Operates on the plain (escape-stripped) pane string; no second capture.
    """
    clean = strip_terminal_escapes(text)
    if KIRO_BUSY_MARKER_PATTERN.search(clean):
        return True
    if re.search(NEW_TUI_IDLE_PATTERN, clean):
        return False
    return None


# TUI permission prompt: shown instead of legacy [y/n/t] format.
# Requires all three options together to avoid false positives on "Yes"/"No" in agent output.
# Kiro 2.11 renders the same three-way choice with different wording for
# subagent spawning: "Yes, single permission / Trust, always allow in this
# session / No (Tab to edit)". Both alternatives anchor on the full Yes/Trust/No
# option layout so agent output that merely mentions a permission prompt (or
# quotes "subagent requires approval") can't flip status to WAITING_USER_ANSWER
# — the bare header alternative was dropped for that reason. In practice
# --trust-all-tools suppresses this prompt anyway; detection is a safety net.
TUI_PERMISSION_PATTERN = (
    r"Yes\s+No\s+Always [Aa]llow"
    r"|Yes,\s*single permission[\s\S]{0,200}?Trust,\s*always allow[\s\S]{0,200}?No"
)

# TUI trust-all-tools acceptance dialog: shown at startup when --trust-all-tools is passed.
# Matches the footer navigation chrome to avoid false positives on warning text in agent output.
# Must be anchored to bottom screen region (see get_status WAITING check) to avoid stale matches.
TUI_TRUST_ALL_TOOLS_FOOTER = r"esc to cancel · ↑↓ to navigate · ↵ to select"

# Distinctive consent-dialog body text. TUI_TRUST_ALL_TOOLS_FOOTER above is
# kiro's GENERIC list-selector chrome — an update/login/onboarding selector
# renders it identically — so before answering we require this trust-specific
# line, and require the ❯ cursor to sit on "No, exit" so Down lands on
# "Yes, I accept" (not "Yes, and don't ask again"). Anything else fails closed.
TUI_TRUST_ALL_TOOLS_BODY = r"Kiro is running in trust all tools mode"
TUI_TRUST_ALL_TOOLS_CURSOR = r"❯\s*No, exit"

# Bottom pane lines scanned for the consent dialog (mirrors codex's window).
STARTUP_PROMPT_BOTTOM_LINES = 15

# =============================================================================
# Error Detection
# =============================================================================

# Strings that indicate the agent encountered an error
ERROR_INDICATORS = ["Kiro is having trouble responding right now"]

# F560: session-lock banner. Kiro may refuse to resume a session id that is
# still open in another live process, printing a line naming the holding PID.
# On kiro-cli 2.20.1 no hard lock was observed for a concurrent --resume-id
# (both processes attached), so this is a DEFENSIVE detector, not a verified
# path: if a future/other build does emit such a banner during init, CAO
# surfaces a clear E-KIRO-SESSION-LOCKED error instead of a bare init timeout.
# In normal F444 wake the supervisor reaps the old worker before resuming, so
# the lock (if any) is already released.
SESSION_LOCKED_PATTERN = (
    r"[Ss]ession is (?:active|already open|in use)(?:[^.\n]*)"
    r"(?:another process|PID\s*\d+|process\s*\d+)"
)
E_KIRO_SESSION_LOCKED = "E-KIRO-SESSION-LOCKED"


class KiroCliProvider(BaseProvider):
    condition_provider_key = "kiro_cli"  # F611 #467
    supports_screen_detection = True  # F110: auto-responder opt-in (G7 R2 root cause)
    # F566 (fixes F560): kiro session identity is HARVESTED, never minted.
    # Empirical rule (kiro-cli 2.20.1, probe report
    # /data/cao-scratch/kiro-mcp-probe/report.md): `--resume-id <id>` naming a
    # session that does NOT exist on disk makes kiro take
    # `session.load.create_uncreated` — it materialises an empty session stub
    # and SILENTLY IGNORES `--agent <profile>`, so no profile is applied and
    # 0 MCP servers are prepared (the worker boots with no cao-mcp-server
    # tools). `--resume-id` naming an existing session resumes it correctly,
    # WITH the profile and its MCP servers. kiro has no flag to start a fresh
    # session under a caller-chosen id (see `kiro-cli --v3 chat --help`).
    #
    # Therefore: a FRESH spawn launches WITHOUT `--resume-id`, and its provider
    # session id is UNKNOWN until harvested (allocated_session_uuid is None;
    # terminal creation persists provider_session_id = NULL). `--resume-id` is
    # passed ONLY when re-attaching to an id previously observed real (the
    # fork_context mode=="resume" path), which is what makes F444 #299
    # hibernate/wake work once the real id is known.
    #
    # Harvesting the real id after spawn (so a fresh worker becomes wakeable)
    # is tracked separately as #416 pt2 and is NOT implemented here.
    #
    # Deliberately NOT supports_reauth_rebind: that seam captures+validates a
    # session artifact at init, but a fresh spawn has no session artifact until
    # the first turn, so capture would spuriously fail. Mirrors claude_code,
    # which also does not opt into the capture seam.
    """Provider for Kiro CLI tool integration.

    This provider manages the lifecycle of a Kiro CLI chat session within a tmux window,
    including initialization, status detection, and response extraction.

    Attributes:
        terminal_id: Unique identifier for this terminal instance
        session_name: Name of the tmux session containing this terminal
        window_name: Name of the tmux window for this terminal
        _agent_profile: Name of the Kiro agent profile to use
        _idle_prompt_pattern: Regex pattern for detecting IDLE state
        _permission_prompt_pattern: Regex pattern for detecting permission prompts
    """

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: str,
        allowed_tools: Optional[list] = None,
        engine: Optional[KiroEngine] = None,
        model: Optional[str] = None,
        skill_prompt: Optional[str] = None,
        fork_context: Optional[ForkContext] = None,
    ):
        """Initialize Kiro CLI provider with terminal context.

        Args:
            terminal_id: Unique identifier for this terminal
            session_name: Name of the tmux session
            window_name: Name of the tmux window
            agent_profile: Name of the Kiro agent profile to use (e.g., "developer")
            allowed_tools: Optional list of CAO tool names the agent is allowed to use
            engine: Resolved Kiro engine. Terminal creation probes it before this provider exists.
            model: Explicit per-call override for profile.model (see
                _get_profile_model), e.g. a handoff/assign caller pinning a
                specific model for one worker without a dedicated profile.
            skill_prompt: Optional skill catalog text (unused by kiro today;
                accepted for constructor parity with fork-capable providers).
            fork_context: F560 — when present with mode=="resume", carries the
                prior session id so initialize() relaunches with --resume-id.
        """
        super().__init__(
            terminal_id,
            session_name,
            window_name,
            allowed_tools,
            skill_prompt=skill_prompt,
            fork_context=fork_context,
        )
        self._initialized = False
        self._input_received = False
        self._agent_profile = agent_profile
        self._engine = resolve_kiro_engine(persisted=engine)
        self._model = model
        # F566: session id identity. On resume the prior (real, previously
        # observed) id is reused verbatim, so kiro re-opens that conversation
        # with its profile and MCP servers. On a fresh spawn the id is UNKNOWN
        # — kiro allocates it itself and CAO cannot choose it — so this stays
        # None and no `--resume-id` is passed (see the class comment: a minted
        # id is by definition uncreated, and an uncreated `--resume-id` strips
        # the agent profile and all MCP servers).
        if fork_context is not None and fork_context.mode == "resume":
            self.allocated_session_uuid: Optional[str] = fork_context.session_uuid
        else:
            self.allocated_session_uuid = None

        # Build dynamic prompt pattern based on agent profile
        # This pattern matches various Kiro prompt formats after ANSI stripping:
        # - [developer] >       (basic prompt)
        # - [developer] !>      (prompt with pending changes)
        # - [developer] 50% >   (prompt with progress indicator)
        # - [developer] λ >     (prompt with lambda symbol)
        # - [developer] 50% λ > (combined progress and lambda)
        self._idle_prompt_pattern = (
            rf"\[{re.escape(self._agent_profile)}\]\s*(?:\d+%\s*)?(?:\u03bb\s*)?!?>\s*"
        )
        self._permission_prompt_pattern = r"Allow this action\?.*?\[.*?y.*?/.*?n.*?/.*?t.*?\]:"

        # New TUI header pattern: "agent_name · model · ◔ N%"
        self._new_tui_header_pattern = rf"{re.escape(self._agent_profile)}\s+·\s+.*·\s+◔\s*\d+%"

    @property
    def resolved_model(self) -> Optional[str]:
        """Return the effective model resolved by the service layer."""
        return getattr(self, "_model", None)

    def resume_session_uuid(self) -> str | None:
        """F560: the resume seed, used by the create path's settlement logic.

        Returns the prior session id when this terminal was created to resume
        (assign(resume=True) / fork_from=<uuid>), else None. F566: on a fresh
        spawn there is no id to persist — the create path writes
        provider_session_id = NULL (resume_uuid or allocated_uuid, both None)
        until the real id is harvested (#416 pt2).
        """
        if self._fork_context is not None and self._fork_context.mode == "resume":
            return self._fork_context.session_uuid
        return None

    def _resume_session_id(self) -> Optional[str]:
        """The ``--resume-id`` value for launch, or None to omit the flag.

        F566: only a REAL (previously observed) session id may be passed.
        That is exactly the resume path's prior id; a fresh spawn returns None
        so the launch omits ``--resume-id`` entirely. Passing an id kiro has
        never created makes it take ``session.load.create_uncreated``, which
        silently ignores ``--agent`` and prepares 0 MCP servers.
        """
        return self.allocated_session_uuid

    def capture_session_uuid(self, pane_pid: int, launch_time: float, cwd: str) -> str:
        """F566: return the known session id, else capture it from kiro.

        On a resume the id is already known (the prior real id) and is returned
        without shelling out. On a fresh spawn ``allocated_session_uuid`` is
        None — kiro chose the id itself — so the list-sessions capture below is
        the only way to learn it. ``pane_pid`` is unused. Note this method is
        not on the create path today: kiro is not ``supports_reauth_rebind``,
        so ``_persist_provider_runtime_identity`` returns before reaching it;
        wiring a post-spawn harvest is #416 pt2.
        """
        if isinstance(self.allocated_session_uuid, str) and self.allocated_session_uuid:
            return self.allocated_session_uuid
        # Legacy fallback: no minted id (should not happen for new terminals).
        from cli_agent_orchestrator.services.fork_context_service import capture_kiro_uuid

        return capture_kiro_uuid(self._engine == KiroEngine.KAS, launch_time, cwd)

    def _detect_session_lock(self) -> bool:
        """F560: return True if the pane shows a session-locked banner.

        Best-effort, read-only scan of current pane history for the
        SESSION_LOCKED_PATTERN. Used only to turn an otherwise-opaque init
        stall into a clear E-KIRO-SESSION-LOCKED error when resuming an id that
        another live process still holds.
        """
        try:
            history = get_backend().get_history(self.session_name, self.window_name)
        except Exception:
            return False
        clean = re.sub(ANSI_CODE_PATTERN, "", history or "")
        return re.search(SESSION_LOCKED_PATTERN, clean) is not None

    @property
    def paste_enter_count(self) -> int:
        """Kiro CLI 2.11 needs 2 Enters after bracketed paste to submit.

        The first Enter is consumed by the TUI (finalizes the paste), the
        second submits the message. Older kiro versions only needed 1.
        """
        return 2

    @property
    def paste_submit_delay(self) -> float:
        """Kiro 2.11's TUI needs a longer delay after bracketed paste before
        the Enter key registers as submit rather than being swallowed."""
        return 1.0

    def _after_dispatch_commit_locked(self) -> None:
        """Track that input was sent, enabling separator-free completion detection."""
        self._input_received = True

    @property
    def extraction_tail_lines(self) -> int:
        """Capture enough scrollback for no-credits extraction.

        The no-credits fallback (_extract_tui_message) needs both the
        start_separator (before the response) and end_separator (TUI frame
        before the idle prompt) in the same capture window. For long agent
        responses the start_separator can be hundreds of lines above the
        idle prompt. 2000 lines covers responses up to ~1800 lines of
        content, which exceeds any realistic single-turn agent response.
        """
        return 2000

    def _get_profile_model(self) -> Optional[str]:
        """Resolve model: spawn override > providers.toml > profile field.

        F107 B2: kiro joins codex/grok/claude on resolve_provider_string_option
        so ``[kiro_cli] model = "auto"`` in providers.toml is honored.

        Best-effort: historically the Kiro CLI provider has not required the
        CAO agent profile to be loadable at runtime (kiro-cli has its own
        agent store). A missing or unparseable profile must not block launch;
        toml defaults still apply when the profile is absent.
        """
        if self._model:
            return self._model
        profile = None
        try:
            profile = load_agent_profile(self._agent_profile)
        except (FileNotFoundError, RuntimeError) as exc:
            logger.debug(
                "Profile '%s' not loadable by CAO; falling back to providers.toml: %s",
                self._agent_profile,
                exc,
            )
        provider_defaults = get_provider_defaults("kiro_cli")
        profile_name = getattr(profile, "name", None) or self._agent_profile
        profile_defaults = get_provider_profile_defaults(provider_defaults, profile_name)
        return resolve_provider_string_option(
            profile_defaults,
            provider_defaults,
            profile,
            "model",
            "model",
        )

    def _assert_kiro_identity_guard(self) -> None:
        """F118 §7 loud identity guard — runs at the top of initialize().

        Raises on:
        (a) Profile declares a provider != kiro_cli (misroute from explicit param).
        (b) Base agent JSON missing (profile never installed for kiro → kiro_default).
        """
        # (a) Provider-routing mismatch: profile says it belongs to another provider.
        try:
            profile = load_agent_profile(self._agent_profile)
            if profile.provider and profile.provider != "kiro_cli":
                raise RuntimeError(
                    f"Provider routing mismatch: profile '{self._agent_profile}' declares "
                    f"provider='{profile.provider}' but was routed to kiro_cli. Refusing to "
                    f"launch — fix the routing or the profile's provider field."
                )
        except (FileNotFoundError, RuntimeError) as exc:
            # If we can't load the profile at all, that's fine for the mismatch
            # check — the base-exists check below is what matters.
            if "routing mismatch" in str(exc):
                raise
            logger.debug(
                "Profile '%s' not loadable for mismatch check: %s",
                self._agent_profile,
                exc,
            )

        # (b) Base agent JSON must exist (installed by `cao install`).
        base = KIRO_AGENTS_DIR / f"{self._agent_profile.replace('/', '__')}.json"
        if not base.exists():
            raise RuntimeError(
                f"kiro base agent JSON missing: {base} (profile '{self._agent_profile}' has no "
                f"installed kiro agent; re-run `cao install` for a kiro variant, or fix the "
                f"provider routing). Refusing to launch unprofiled kiro_default."
            )

    def _assert_postlaunch_identity(self) -> None:
        """F118 §7.2 post-launch assertion — verify status bar shows expected agent.

        Polls the pane buffer for the kiro not-found banner or kiro_default status.
        Raises on mismatch so the launch fails loudly instead of running unprofiled.
        """
        pane_content = get_backend().capture_viewport(self.session_name, self.window_name)
        if not pane_content:
            return  # No content yet — skip (timeout will catch real failures)

        # Check for the "agent not found" banner
        not_found_pattern = r'agent ".*?" not found, using "kiro_default"'
        if re.search(not_found_pattern, pane_content):
            raise RuntimeError(
                f"kiro-cli launched with kiro_default (agent '{self._agent_profile}' not found "
                f"in kiro registry). The base agent JSON may be corrupt or the name field "
                f"doesn't match. Re-run `cao install`."
            )

        # Check the TUI header for agent name — if it shows a different agent, fail.
        # Only assert if we can positively see the header (absence is not a failure).
        if re.search(r"kiro_default\s+·", pane_content):
            raise RuntimeError(
                f"kiro-cli status bar shows 'kiro_default' instead of "
                f"'{self._agent_profile}'. The agent JSON name field may not match "
                f"the profile name. Re-run `cao install`."
            )

    async def initialize(self) -> bool:
        """Initialize Kiro CLI provider by starting kiro-cli chat command.

        This method:
        1. Waits for the shell to be ready in the tmux window
        2. Sends the kiro-cli chat command with the configured agent profile
        3. Waits for the agent to reach IDLE state (ready for input)

        Returns:
            True if initialization was successful

        Raises:
            TimeoutError: If shell or Kiro CLI initialization times out
        """
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        # F118 §7: Loud identity guard — fail fast on misroute or missing base JSON.
        self._assert_kiro_identity_guard()

        # Step 1: Wait for shell prompt to appear in the tmux window
        # This ensures the terminal is ready before we send commands
        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        # Capture the shell process name before launching kiro — used later to detect kiro exit
        self.shell_baseline = get_backend().get_pane_current_command(
            self.session_name, self.window_name
        )

        # Step 2: Start the Kiro CLI chat session.
        #
        # --trust-all-tools: bypass Kiro CLI's permission prompts when CAO
        # launches with --yolo (allowed_tools=['*']). Without this, every
        # tool invocation re-prompts, blocking assign/handoff flows.
        # --model: honor profile.model so workflows can pin a specific model.
        #
        # UI mode selection (OUR behaviour, preserved over upstream #691):
        # - Yolo (--trust-all-tools): kiro-cli 2.0.1 TUI blocks on an
        #   interactive "Yes, I accept" consent dialog before the chat is
        #   ready; only --legacy-ui/--classic/--no-interactive bypass it.
        #   CAO drives kiro-cli headlessly, so we force --legacy-ui for yolo.
        # - Non-yolo: use the default TUI (fall back to --legacy-ui on
        #   timeout, preserving prior behavior for older kiro-cli versions).
        yolo = bool(self._allowed_tools and "*" in self._allowed_tools)
        model = self._get_profile_model()
        # F566: pass --resume-id ONLY when re-attaching to a real prior session
        # id (the resume path). None on a fresh spawn, so build_kiro_command
        # omits the flag and kiro takes session/new — which applies --agent and
        # prepares the profile's MCP servers. An uncreated --resume-id would
        # instead take session.load.create_uncreated: no profile, 0 MCP servers
        # (kiro-cli 2.20.1, /data/cao-scratch/kiro-mcp-probe/report.md).
        resume_id = self._resume_session_id()

        # kiro-cli 2.11 introduced a "subagent requires approval" prompt that
        # blocks MCP tool calls that spawn subagents (e.g. cao-mcp-server's
        # assign/handoff). Even for non-yolo profiles, CAO enforces tool
        # scoping at its own layers (profile allowedTools + MCP allowlist),
        # so passing --trust-all-tools is safe: it bypasses kiro's
        # per-invocation UI prompt (there is no human at the terminal in
        # headless orchestration to answer), while CAO still gates what
        # tools can be called. Without this, a supervisor invoking assign()
        # hangs indefinitely on the approval dialog.
        if yolo:
            logger.info(
                "kiro_cli yolo mode: forcing --legacy-ui (kiro-cli 2.0.1 TUI "
                "shows a non-bypassable trust-all-tools consent dialog)"
            )
            base_args = build_kiro_command(
                self._engine,
                self._agent_profile,
                model=model,
                yolo=True,
                legacy_ui=True,
                resume_session_id=resume_id,
            )
        else:
            # Current CAO policy always bypasses Kiro's interactive approval
            # prompt; CAO still enforces the profile/MCP allowlist itself.
            base_args = build_kiro_command(
                self._engine,
                self._agent_profile,
                model=model,
                yolo=True,
                resume_session_id=resume_id,
            )
        command = shlex.join(base_args)
        # Arm the StatusMonitor stickiness gate before launching the CLI so
        # the IDLE → PROCESSING → IDLE/COMPLETED transition is honored.
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Step 3: Wait for Kiro CLI to fully initialize and show the agent prompt.
        # Accept both IDLE and COMPLETED — some CLI versions show a startup
        # message that get_status() interprets as a completed response.
        #
        # F109: Build a BlockedWaitPolicy (mirrors codex.py) so that named
        # auto-responder wait rules pause the init deadline instead of burning it.
        from cli_agent_orchestrator.services.auto_responder import auto_responder

        async def notify_blocked(rule_name: str) -> None:
            if self.blocked_wait_notifier is not None:
                await self.blocked_wait_notifier(rule_name)

        def probe_blocked() -> tuple[str, str] | None:
            gate = auto_responder.waiting_gate(self.terminal_id)
            return gate if isinstance(gate, tuple) else None

        blocked_policy = BlockedWaitPolicy(
            probe=probe_blocked,
            blocked_cap_s=BLOCKED_WAIT_CAP_S,
            on_first_blocked=notify_blocked,
        )

        # _wait_ready_accepting_trust_dialog also auto-answers the
        # --trust-all-tools startup consent dialog (see its docstring), which
        # kiro-cli >= 2.1 shows in the default TUI *and* under --legacy-ui.
        if not await self._wait_ready_accepting_trust_dialog(blocked_policy=blocked_policy):
            # F560: if this launch resumed a prior id and the pane shows a
            # session-locked banner, surface a clear error rather than a bare
            # timeout — the id is still held by another live process (should
            # not happen once the old worker is reaped before wake).
            # F566: only a launch that actually passed --resume-id can be
            # blocked by another holder of that id, so gate on resume_id (not
            # merely on fork_context) — a fresh spawn has no id to report.
            if resume_id and self._detect_session_lock():
                raise RuntimeError(
                    f"{E_KIRO_SESSION_LOCKED}: kiro session "
                    f"'{resume_id}' is still open in another "
                    f"process; reap the prior worker before resuming."
                )
            if yolo:
                # Yolo already launched with --legacy-ui; no further fallback.
                suffix = (
                    f" after blocked wait rule '{blocked_policy.last_blocked_rule}'"
                    if blocked_policy.last_blocked_rule
                    else ""
                )
                raise TimeoutError(
                    f"Kiro CLI initialization timed out with --legacy-ui (yolo mode){suffix}"
                )
            # Non-yolo TUI mode failed — fall back to --legacy-ui
            logger.warning("Kiro CLI TUI initialization timed out, retrying with --legacy-ui")
            # Exit the current session and start fresh with --legacy-ui
            status_monitor.notify_input_sent(self.terminal_id)
            get_backend().send_keys(self.session_name, self.window_name, "/exit")
            init_timeout_shell = get_server_settings()["provider_init_timeout"]
            if not await wait_for_shell(self.terminal_id, timeout=init_timeout_shell):
                raise TimeoutError(
                    f"Shell recovery timed out after {init_timeout_shell}s (--legacy-ui fallback)"
                )
            # Clear the StatusMonitor buffer so the --legacy-ui attempt is detected
            # against a clean buffer, not one still full of stale TUI marker bytes
            # from the failed first attempt (which would otherwise time out too).
            status_monitor.reset_buffer(self.terminal_id)
            legacy_args = build_kiro_command(
                self._engine,
                self._agent_profile,
                model=model,
                yolo=True,
                legacy_ui=True,
                resume_session_id=resume_id,
            )
            legacy_command = shlex.join(legacy_args)
            status_monitor.notify_input_sent(self.terminal_id)
            get_backend().send_keys(self.session_name, self.window_name, legacy_command)
            # Reset last_blocked_rule so a rule from the first wait doesn't
            # falsely label a second-wait timeout that never parked (fold r1/D1).
            blocked_policy.last_blocked_rule = None
            if not await self._wait_ready_accepting_trust_dialog(blocked_policy=blocked_policy):
                suffix = (
                    f" after blocked wait rule '{blocked_policy.last_blocked_rule}'"
                    if blocked_policy.last_blocked_rule
                    else ""
                )
                raise TimeoutError(
                    f"Kiro CLI initialization timed out with TUI and `--legacy-ui`{suffix}"
                )

        # F118 §7.2: Post-launch identity assertion — catch kiro_default fallback.
        self._assert_postlaunch_identity()

        self._initialized = True
        return True

    def classify_injection_hazard(self, rows: list[str]) -> str | None:
        return (
            "interactive_dialog"
            if self.get_status_from_screen(rows) == TerminalStatus.WAITING_USER_ANSWER
            else None
        )

    async def _wait_ready_accepting_trust_dialog(
        self, *, blocked_policy: "BlockedWaitPolicy | None" = None
    ) -> bool:
        """Wait for the agent prompt, auto-answering the trust-all-tools dialog.

        CAO always launches kiro-cli with ``--trust-all-tools`` (there is no
        human at the terminal to answer per-tool permission prompts in headless
        orchestration; CAO enforces tool scoping at its own profile/MCP layers).
        Since kiro-cli 2.1, ``--trust-all-tools`` opens a one-time startup
        consent dialog *before* the chat prompt is interactive:

            ❯ No, exit
              Yes, I accept
              Yes, and don't ask again

        The default TUI shows this dialog on kiro-cli 2.16.1 (verified), so the
        earlier "force --legacy-ui to skip it" workaround no longer helps and
        init just times out on the dialog. This helper is applied to the
        ``--legacy-ui`` fallback path too, so if a kiro build shows the dialog
        there as well it is handled without a version check. get_status()
        classifies the dialog as WAITING_USER_ANSWER off generic selector
        chrome, so before answering we VERIFY the dialog body and the ❯ cursor
        line (fail closed on anything else), then select **"Yes, I accept"**
        (Down, Enter) — the one-line-down option. We deliberately do NOT pick
        "Yes, and don't ask again", which would persist a trust-all-tools
        bypass into the user's kiro config; CAO's acceptance is scoped to this
        ephemeral session only.

        Returns:
            True if the terminal reached IDLE or COMPLETED (dialog answered if
            it was verified and shown), False if it timed out or a
            WAITING_USER_ANSWER could not be verified as the consent dialog.
        """
        init_timeout = float(get_server_settings()["provider_init_timeout"])
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        start = time.monotonic()
        ready = await wait_until_status(
            self.terminal_id,
            {
                TerminalStatus.IDLE,
                TerminalStatus.COMPLETED,
                TerminalStatus.WAITING_USER_ANSWER,
            },
            timeout=init_timeout,
            blocked_policy=blocked_policy,
        )
        if not ready:
            return False
        # Ready-flap window: wait_until_status returned on WAITING_USER_ANSWER,
        # but get_status is re-read here rather than trusted from the wait. If it
        # has since flapped to IDLE/COMPLETED, treat the terminal as ready and do
        # NOT send dialog keys (a blind Down+Enter into a live prompt would be a
        # stray message). Low probability given the sticky latch, but explicit.
        # Off the loop: get_status() can fork a tmux capture-pane for a PROCESSING
        # terminal (status_monitor.py's stale-PROCESSING fallback).
        current = await asyncio.to_thread(status_monitor.get_status, self.terminal_id)
        if current != TerminalStatus.WAITING_USER_ANSWER:
            return True

        # WAITING_USER_ANSWER is classified from TUI_TRUST_ALL_TOOLS_FOOTER,
        # which is kiro's GENERIC list-selector chrome. Verify the dialog BODY
        # and the ❯ cursor position before answering — a blind Down+Enter on
        # some other startup selector would pick an arbitrary option. Same
        # fail-closed shape as codex's directory-trust check. On any mismatch,
        # fall through to the normal timeout/fallback path.
        backend = get_backend()
        pane = strip_terminal_escapes(
            re.sub(ANSI_CODE_PATTERN, "", backend.get_history(self.session_name, self.window_name))
        )
        bottom = "\n".join(pane.splitlines()[-STARTUP_PROMPT_BOTTOM_LINES:])
        if not (
            re.search(TUI_TRUST_ALL_TOOLS_BODY, bottom)
            and re.search(TUI_TRUST_ALL_TOOLS_CURSOR, bottom)
        ):
            logger.warning(
                "kiro_cli: WAITING_USER_ANSWER but the trust-all-tools consent "
                "dialog (body + '❯ No, exit') was not verified; not answering"
            )
            return False

        # Verified consent dialog: cursor on "No, exit", so Down lands on
        # "Yes, I accept" (session-scoped — NOT "Yes, and don't ask again").
        logger.info(
            "kiro_cli: answering --trust-all-tools startup consent dialog "
            "with 'Yes, I accept' (session-scoped)"
        )
        # Arm the PROCESSING latch before the keystrokes, like every other
        # send_special_key path, so answering the dialog isn't blocked by the
        # WAITING_USER_ANSWER we just observed.
        status_monitor.notify_input_sent(self.terminal_id)
        backend.send_special_key(self.session_name, self.window_name, "Down")
        backend.send_special_key(self.session_name, self.window_name, "Enter")
        # Bound the post-accept wait by the time already spent, so total startup
        # stays within provider_init_timeout rather than up to 2x it.
        remaining = max(0.0, init_timeout - (time.monotonic() - start))
        return await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=remaining,
            blocked_policy=blocked_policy,
        )

    def rule3a_busy_marker(self, snapshot: str) -> bool | None:
        """F581 D16 busy-marker for rule 3a: is the kiro seat's own TUI activity
        marker live? Delegates to the shared pure helper ``kiro_busy_marker_live``.

        ``snapshot`` is the plain pane string the liveness sampler already holds;
        no second capture. Returns ``True``/``False``/``None`` per the D12d truth
        table (see the helper). Proven from the byte-exact fixtures under
        ``test/providers/fixtures/busy_marker/kiro_cli/``.
        """
        return kiro_busy_marker_live(snapshot)

    def get_status(self, output: str) -> TerminalStatus:
        """Get Kiro CLI status by analyzing terminal output.

        Status detection logic (in priority order):
        1. No output → UNKNOWN
        2. No IDLE prompt visible → PROCESSING (agent is generating response)
        3. Error indicators present → ERROR
        4. Permission prompt visible → WAITING_USER_ANSWER
        5. Green arrow + prompt visible → COMPLETED (response ready)
        6. Only prompt visible → IDLE (waiting for input)

        Native (herdr): if the backend can report a native agent_status, trust it
        and skip buffer parsing. On herdr the pipe-pane buffer is never fed, so
        ``output`` is empty and the regex path below can never leave UNKNOWN —
        which is why kiro never reached IDLE and init timed out. The shared
        BaseProvider helper consults the backend and disambiguates herdr's
        ambiguous "idle" via _task_dispatched (set by mark_input_received).

        F107 B3: engine-branched. v2 path is unchanged; KAS uses a dedicated
        seat with best-effort v3 guesses until G7 live sampling (E1) pins
        the final chrome strings.
        """
        native = self._resolve_native_status(output)
        if native is not None:
            return native

        # herdr never pushes a buffer (pipe_pane is a no-op there); read live
        # pane content instead of falling through to "no output" on every call.
        output = self._resolve_buffer(output)
        if not output:
            return TerminalStatus.UNKNOWN

        if self._engine == KiroEngine.KAS:
            return self._get_status_kas(output)
        return self._get_status_v2(output)

    def _get_status_kas(self, output: str) -> TerminalStatus:
        """F107 B3 KAS (--v3) classifier seat.

        Best-effort provisional chrome until G7 live sampling (E1) pins the
        final strings. Known/guessed KAS markers are checked first; anything
        unmatched falls through to the v2 classifier as a provisional baseline
        (v3 agent configs are backward-compatible; chrome may still diverge).
        """
        # Best-effort v3 guesses (provisional — G7 E1 replaces/extends these):
        # - "Thinking..." already covered by TUI_PROCESSING_PATTERN in the v2 body
        # - possible alternate working chrome observed on early KAS builds
        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)
        kas_working = re.search(
            r"Working on (?:your |the )?request|Agent is thinking|Generating response",
            clean_output,
            re.IGNORECASE,
        )
        if kas_working:
            # Only park as PROCESSING when no idle chrome follows the marker
            # (same ghost-text discipline as the v2 working check).
            after = clean_output[kas_working.end() :]
            if not (
                re.search(self._idle_prompt_pattern, after)
                or re.search(NEW_TUI_IDLE_PATTERN, after)
            ):
                return TerminalStatus.PROCESSING
        # Provisional baseline: reuse v2 classification for shared chrome.
        return self._get_status_v2(output)

    def _get_status_v2(self, output: str) -> TerminalStatus:
        """v2 / legacy-UI status classifier (byte-identical pre-F107 behavior)."""
        # Strip ONLY SGR colour codes for pattern matching. Carriage returns and
        # cursor-movement sequences are intentionally preserved: the permission
        # check below counts idle prompts per "\n"-delimited line and relies on
        # \r in-place redraws staying on the same logical line (see
        # ANSI_CODE_PATTERN). Do not switch this to strip_terminal_escapes.
        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

        # Check 0a: Detect idle prompts early — required for the position-aware
        # processing checks below.
        old_idle_matches = list(re.finditer(self._idle_prompt_pattern, clean_output))
        new_tui_idle_matches = list(re.finditer(NEW_TUI_IDLE_PATTERN, clean_output))
        has_idle_prompt = old_idle_matches[0] if old_idle_matches else None
        has_new_tui_idle = bool(new_tui_idle_matches)

        # Check 0b: TUI startup — Kiro emits "● Initializing..." or
        # "0 of N mcp servers initialized. ctrl-c to start chatting now"
        # before the prompt is interactive; pastes during this window are
        # silently absorbed by the boot screen.
        #
        # The new TUI renders an idle-prompt PLACEHOLDER ("Ask a question
        # or describe a task") even during boot, so NEW_TUI_IDLE_PATTERN
        # matching after the init line does NOT mean init has finished —
        # we must still report PROCESSING.
        #
        # In --legacy-ui (and once the new TUI is interactive), the actual
        # "[agent] N% > " idle prompt only appears AFTER init has completed.
        # The "0 of N mcp servers initialized..." line is drawn once at
        # boot and redrawn over by the TUI; under the event-driven FIFO
        # pipeline that line still sits in the rolling byte stream forever
        # (issue surfaced by yolo --legacy-ui timing out 11/11 e2e tests).
        # Treat the init line as PROCESSING only when no real ``[agent] >``
        # idle prompt appears AFTER the last init match — mirrors the
        # TUI_PROCESSING_PATTERN ghost-text guard below.
        #
        # kiro-cli 2.8.x TUI shows "● Initializing..." (animated spinner)
        # during MCP boot. Once MCP finishes, the TUI redraws completely:
        # the spinner disappears and the idle prompt appears. In the raw
        # FIFO buffer, the idle prompt text lands AFTER the last spinner
        # frame, so checking new_tui_idle_matches after last_init_pos is a
        # reliable post-init signal. During the spinner, only spinner frames
        # are written to the stream; the idle prompt only enters the buffer
        # when the TUI redraws after init completes.
        init_matches = list(re.finditer(TUI_INITIALIZING_PATTERN, clean_output))
        if init_matches:
            last_init_pos = init_matches[-1].end()
            real_idle_after_init = any(m.start() > last_init_pos for m in old_idle_matches)
            new_idle_after_init = any(m.start() > last_init_pos for m in new_tui_idle_matches)
            if not real_idle_after_init and not new_idle_after_init:
                return TerminalStatus.PROCESSING

        # Check 2: Look for TUI "Kiro is working" ghost text.
        # Kiro TUI redraws the screen in-place, so the buffer can retain a stale
        # "Kiro is working" line from an earlier render even after the agent has
        # finished and the idle prompt has appeared below it.  Only return
        # PROCESSING when no idle prompt appears *after* the last match.
        tui_working_matches = list(re.finditer(TUI_PROCESSING_PATTERN, clean_output))
        if tui_working_matches:
            last_working_pos = tui_working_matches[-1].end()
            idle_after_working = any(
                m.start() > last_working_pos for m in new_tui_idle_matches + old_idle_matches
            )
            if not idle_after_working:
                return TerminalStatus.PROCESSING

        # Check 2a: Trust-all-tools acceptance dialog at startup (no idle prompt case).
        # Must come BEFORE the "no idle prompt → PROCESSING" check below, since
        # the dialog has no idle prompt but should classify WAITING_USER_ANSWER.
        # Anchored to bottom 20 lines to avoid false positives when the warning text
        # appears in scrollback with an idle prompt below (same class as issue #405).
        lines = clean_output.split("\n")
        bottom_region = "\n".join(lines[-20:]) if len(lines) > 20 else clean_output
        footer_match = re.search(TUI_TRUST_ALL_TOOLS_FOOTER, bottom_region)
        if footer_match:
            after_footer = bottom_region[footer_match.end() :]
            has_idle_after = re.search(self._idle_prompt_pattern, after_footer) or re.search(
                NEW_TUI_IDLE_PATTERN, after_footer
            )
            if not has_idle_after:
                return TerminalStatus.WAITING_USER_ANSWER

        # Check 3: If no idle prompt found, determine if kiro is still running.
        # Compare current pane command against the shell captured before kiro launched.
        # If they match, kiro has exited and the shell is showing again → IDLE.
        #
        # Gated on self._initialized: between send_keys("kiro-cli chat ...")
        # and the moment kiro-cli exec's, the pane's current command still
        # matches shell_baseline ("zsh"), and the buffer hasn't shown any
        # idle prompt yet. Without this gate, get_status() returns IDLE
        # immediately after launch, which lets pre-init pastes get absorbed
        # by Kiro's boot screen and silently dropped.
        if not has_idle_prompt and not has_new_tui_idle:
            if self._initialized and self.shell_baseline:
                current_cmd = get_backend().get_pane_current_command(
                    self.session_name, self.window_name
                )
                if current_cmd == self.shell_baseline:
                    return TerminalStatus.IDLE
            return TerminalStatus.PROCESSING

        # Check 2: Look for known error messages in the output
        if any(indicator.lower() in clean_output.lower() for indicator in ERROR_INDICATORS):
            return TerminalStatus.ERROR

        # Check for permission prompt — legacy [y/n/t] or TUI "Yes, No, Always Allow"
        # Active prompt: 0-1 lines with idle prompt (CLI renders prompt on next line)
        # Stale prompt: 2+ lines with idle prompt (user answered, agent continued)
        # Line-based counting handles \r redraws (same line, no \n) correctly
        perm_matches = list(re.finditer(self._permission_prompt_pattern, clean_output, re.DOTALL))
        tui_perm_matches = list(re.finditer(TUI_PERMISSION_PATTERN, clean_output))
        all_perm_matches = perm_matches + tui_perm_matches
        # Sort by position so we use the last permission prompt regardless of type
        all_perm_matches.sort(key=lambda m: m.start())
        if all_perm_matches:
            after_last_perm = clean_output[all_perm_matches[-1].end() :]
            lines_after = after_last_perm.split("\n")
            idle_lines = sum(
                1
                for line in lines_after
                if re.search(self._idle_prompt_pattern, line)
                or re.search(NEW_TUI_IDLE_PATTERN, line)
            )
            if idle_lines <= 1:
                return TerminalStatus.WAITING_USER_ANSWER

        # Check 4: Look for completed response (green arrow indicates agent output)
        # Must verify that an idle prompt appears AFTER the response
        green_arrows = list(re.finditer(GREEN_ARROW_PATTERN, clean_output, re.MULTILINE))
        if green_arrows:
            # Find if there's an idle prompt after the last green arrow
            last_arrow_pos = green_arrows[-1].end()
            idle_prompts = list(re.finditer(self._idle_prompt_pattern, clean_output))

            for prompt in idle_prompts:
                if prompt.start() > last_arrow_pos:
                    logger.debug(f"get_status: returning COMPLETED")
                    return TerminalStatus.COMPLETED

            # Also check new TUI idle pattern after the last green arrow
            for prompt in new_tui_idle_matches:
                if prompt.start() > last_arrow_pos:
                    logger.debug("get_status: returning COMPLETED (new TUI)")
                    return TerminalStatus.COMPLETED

            # Has green arrow but no prompt after it - still processing
            return TerminalStatus.PROCESSING

        # Check 5: TUI completion — Credits marker + idle prompt after it.
        # In pure TUI mode, there are no green arrows. Completion is indicated
        # by "▸ Credits:" followed by the idle prompt.
        credits_matches = list(re.finditer(TUI_CREDITS_PATTERN, clean_output))
        if credits_matches:
            last_credits_pos = credits_matches[-1].end()
            for prompt in new_tui_idle_matches:
                if prompt.start() > last_credits_pos:
                    logger.debug("get_status: returning COMPLETED (TUI credits)")
                    return TerminalStatus.COMPLETED
            for prompt in old_idle_matches:
                if prompt.start() > last_credits_pos:
                    logger.debug("get_status: returning COMPLETED (TUI credits + legacy idle)")
                    return TerminalStatus.COMPLETED
            # Credits marker found but no idle prompt after it — still processing
            return TerminalStatus.PROCESSING

        # Check 6: Kiro CLI 2.3.0+ — no Credits marker emitted. Detect completion
        # by finding the bordered "response box" — two separators with ≥2 lines
        # of non-empty content between them — followed by the idle prompt.
        #
        # kiro-cli 2.11+ keeps the "ask a question or describe a task" placeholder
        # visible in the raw buffer at all times (it's part of the TUI chrome),
        # so a bare idle-prompt match does NOT mean the agent finished. Requiring
        # a full response box (two separators + content in between) distinguishes
        # a real completion frame from the pre-response idle state where the
        # placeholder is visible but the agent hasn't produced output yet.
        # A minimal completion is user query + agent response = 2 non-empty lines
        # inside the box.
        if has_new_tui_idle:
            lines = clean_output.split("\n")

            # Strip non-SGR CSI (erase-line \x1b[2K, cursor moves, etc.) so
            # the separator regex can see the raw ─── characters. The top-of-
            # function strip only removes SGR codes ending in 'm' (color) —
            # kiro's TUI prefixes many lines with \x1b[2K which would prevent
            # the separator regex from anchoring at start-of-line.
            # (_NON_SGR_CSI is compiled once at module scope — hot path.)
            def _sep_line(line: str) -> str:
                return _NON_SGR_CSI.sub("", line).strip()

            idle_line_idx = None
            for i in range(len(lines) - 1, -1, -1):
                if re.search(NEW_TUI_IDLE_PATTERN, lines[i]):
                    idle_line_idx = i
                    break
            if idle_line_idx is not None:
                # Find the last separator BEFORE the idle line.
                last_sep_idx = None
                for i in range(idle_line_idx - 1, -1, -1):
                    if re.search(TUI_SEPARATOR_PATTERN, _sep_line(lines[i])):
                        last_sep_idx = i
                        break
                if last_sep_idx is not None:
                    # Find the previous separator, and require content between them.
                    for j in range(last_sep_idx - 1, -1, -1):
                        if re.search(TUI_SEPARATOR_PATTERN, _sep_line(lines[j])):
                            content = [l for l in lines[j + 1 : last_sep_idx] if _sep_line(l)]
                            if len(content) >= 2:
                                logger.debug(
                                    "get_status: returning COMPLETED (TUI no-credits fallback)"
                                )
                                return TerminalStatus.COMPLETED
                            break

        # Default: Agent is IDLE, waiting for user input
        return TerminalStatus.IDLE

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract agent's final response message using green arrow indicator."""
        # Strip ANSI codes for pattern matching
        clean_output = strip_terminal_escapes(script_output)

        # Find patterns in clean output
        green_arrows = list(re.finditer(GREEN_ARROW_PATTERN, clean_output, re.MULTILINE))
        idle_prompts = list(re.finditer(self._idle_prompt_pattern, clean_output))
        new_tui_idles = list(re.finditer(NEW_TUI_IDLE_PATTERN, clean_output))

        # Slash command fallback: if the most recent interaction (between the
        # last two idle prompts) has no green arrow, it was a CLI-handled
        # command like /context or /compact. Extract that output instead.
        if len(idle_prompts) >= 2:
            last_prompt_pos = idle_prompts[-1].start()
            prev_prompt_pos = idle_prompts[-2].end()
            has_arrow_in_last_interaction = any(
                m.start() > prev_prompt_pos and m.start() < last_prompt_pos for m in green_arrows
            )
            if not has_arrow_in_last_interaction:
                between = clean_output[prev_prompt_pos:last_prompt_pos]
                # First line is the user's command text, skip it
                lines = between.split("\n", 1)
                if lines[0].lstrip().startswith("/"):
                    output = lines[1].strip() if len(lines) > 1 else ""
                    if output:
                        output = re.sub(ESCAPE_SEQUENCE_PATTERN, "", output)
                        output = re.sub(CONTROL_CHAR_PATTERN, "", output)
                        return output.strip()

        if not green_arrows:
            # Fallback: try TUI extraction (separator + Credits pattern)
            return self._extract_tui_message(clean_output)

        if not idle_prompts and not new_tui_idles:
            raise ValueError("Incomplete Kiro CLI response - no final prompt detected")

        # Find the last green arrow (response start)
        last_arrow_pos = green_arrows[-1].end()

        # Find idle prompt that comes AFTER the last green arrow (old or new TUI)
        final_prompt = None
        for prompt in idle_prompts:
            if prompt.start() > last_arrow_pos:
                final_prompt = prompt
                break
        if not final_prompt:
            for prompt in new_tui_idles:
                if prompt.start() > last_arrow_pos:
                    final_prompt = prompt
                    break

        if not final_prompt:
            raise ValueError(
                "Incomplete Kiro CLI response - no final prompt detected after response"
            )

        # Extract directly from clean output
        start_pos = last_arrow_pos
        end_pos = final_prompt.start()

        final_answer = clean_output[start_pos:end_pos].strip()

        if not final_answer:
            raise ValueError("Empty Kiro CLI response - no content found")

        return final_answer.strip()

    def _extract_tui_message(self, clean_output: str) -> str:
        """Extract agent response from pure TUI output (no green arrows).

        TUI format:
            ────────────────────────────
              user message here

              Agent's response here.

            ▸ Credits: 0.24 - Time: 3s
            ────────────────────────────
            agent-name - model - N%
             Ask a question or describe a task

        Strategy:
            1. Find the last Credits line (response end marker)
            2. Find the previous Credits line (prior turn boundary) or start of output
            3. Find the first separator after that boundary (outer TUI separator)
               This avoids matching separators inside the agent's response.
            4. Extract text between separator and Credits
            5. Skip the first paragraph (user message) if a blank line separates it
        """
        lines = clean_output.split("\n")

        # Find the last Credits line
        credits_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if re.search(TUI_CREDITS_PATTERN, lines[i]):
                credits_idx = i
                break

        if credits_idx is None:
            # Kiro CLI 2.3.0+ may not emit a Credits line. Fall back to
            # extracting content between separators around the response.
            # Only attempt this when we know input was sent — without
            # _input_received the original error contract is preserved.
            if self._input_received:
                idle_idx = None
                for i in range(len(lines) - 1, -1, -1):
                    if re.search(NEW_TUI_IDLE_PATTERN, lines[i]):
                        idle_idx = i
                        break

                if idle_idx is not None:
                    # Find the last separator before idle (TUI frame boundary)
                    end_separator_idx = None
                    for i in range(idle_idx - 1, -1, -1):
                        if re.search(TUI_SEPARATOR_PATTERN, lines[i].strip()):
                            end_separator_idx = i
                            break

                    # Find the separator before that (start of response area)
                    start_separator_idx = None
                    if end_separator_idx is not None:
                        for i in range(end_separator_idx - 1, -1, -1):
                            if re.search(TUI_SEPARATOR_PATTERN, lines[i].strip()):
                                start_separator_idx = i
                                break

                    if start_separator_idx is not None and end_separator_idx is not None:
                        content_lines = lines[start_separator_idx + 1 : end_separator_idx]
                        # Skip only the actual TUI header line (agent · model · N%)
                        content_lines = [
                            l
                            for l in content_lines
                            if not re.search(self._new_tui_header_pattern, l)
                        ]
                        # Skip first paragraph (user message echo)
                        agent_start = 0
                        found_blank = False
                        for i, line in enumerate(content_lines):
                            stripped = line.strip()
                            if not found_blank and not stripped:
                                found_blank = True
                                continue
                            if found_blank and stripped:
                                agent_start = i
                                break
                        response_lines = content_lines[agent_start:]
                        final_answer = "\n".join(response_lines).strip()
                        if final_answer:
                            final_answer = re.sub(ESCAPE_SEQUENCE_PATTERN, "", final_answer)
                            final_answer = re.sub(CONTROL_CHAR_PATTERN, "", final_answer)
                            return final_answer.strip()

            raise ValueError(
                "No Kiro CLI response found - no Credits marker or green arrow detected"
            )

        # Find the previous Credits line (prior turn's end) to establish search boundary.
        # This ensures we find the outer TUI separator, not one inside the agent's output.
        prev_credits_idx = -1
        for i in range(credits_idx - 1, -1, -1):
            if re.search(TUI_CREDITS_PATTERN, lines[i]):
                prev_credits_idx = i
                break

        # Find the first separator AFTER the previous turn boundary
        separator_idx = None
        for i in range(prev_credits_idx + 1, credits_idx):
            if re.search(TUI_SEPARATOR_PATTERN, lines[i].strip()):
                separator_idx = i
                break

        # Kiro 2.0: separator is AFTER credits_idx. Scan forward to find it.
        if separator_idx is None:
            next_credits_idx = len(lines)
            for i in range(credits_idx + 1, len(lines)):
                if re.search(TUI_CREDITS_PATTERN, lines[i]):
                    next_credits_idx = i
                    break
            for i in range(credits_idx + 1, next_credits_idx):
                if re.search(TUI_SEPARATOR_PATTERN, lines[i].strip()):
                    separator_idx = i
                    break

        if separator_idx is None:
            raise ValueError("No Kiro CLI response found - no separator found near Credits marker")

        # Extract content between separator and Credits
        if separator_idx > credits_idx:
            # Kiro 2.0: separator after Credits. Content precedes credits_idx.
            content_lines = lines[prev_credits_idx + 1 : credits_idx]
        else:
            # Pre-2.0: separator before Credits (existing behavior)
            content_lines = lines[separator_idx + 1 : credits_idx]

        # Skip the first paragraph (user message echo).
        # The user message is the first block of non-empty lines after the separator.
        # After a blank line, the agent response begins.
        agent_start = 0
        found_blank = False
        for i, line in enumerate(content_lines):
            stripped = line.strip()
            if not found_blank and not stripped:
                found_blank = True
                continue
            if found_blank and stripped:
                agent_start = i
                break

        if not found_blank:
            # No blank line found — entire content is the response
            agent_start = 0

        response_lines = content_lines[agent_start:]
        final_answer = "\n".join(response_lines).strip()

        if not final_answer:
            raise ValueError("Empty Kiro CLI response - no content found")

        # Clean up (ANSI codes already stripped from clean_output at caller)
        final_answer = re.sub(ESCAPE_SEQUENCE_PATTERN, "", final_answer)
        final_answer = re.sub(CONTROL_CHAR_PATTERN, "", final_answer)
        return final_answer.strip()

    def get_idle_pattern_for_log(self) -> str:
        """Return Kiro CLI IDLE prompt pattern for log files.

        Returns a pattern that matches either the legacy UI format
        or the new TUI format.
        """
        from cli_agent_orchestrator.utils.tombstones import tombstone

        tombstone("TS-0002b")
        return rf"(?:{IDLE_PROMPT_PATTERN_LOG}|{NEW_TUI_IDLE_PATTERN_LOG})"

    def exit_cli(self) -> str:
        """Get the command to exit Kiro CLI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Kiro CLI provider."""
        self._initialized = False
