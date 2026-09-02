"""Cline CLI provider implementation — sandbox-mode one-shot invocations.

This provider drives Cline CLI (v3.0.55+) via one-shot command invocations in a
tmux pane.  Each message becomes a fresh ``cline --auto-approve true "<msg>"``
command; the pane output stays human-readable (no --tui, no --json).

Architecture: A lightweight shell dispatcher loop runs in the pane. It accepts
messages via tmux paste (the standard send_input path), writes them to a temp
file for safe escaping, and invokes cline with ``"$(cat <file>)"``.  Status
detection uses the pane's current command: ``cline`` running = PROCESSING,
dispatcher loop idle (at ``read``) = IDLE/COMPLETED.

Session-id correlation: ``cline history --json`` is snapshotted before and after
each invocation (via subprocess, not the pane) to bind the new session record.

Key flags (verified via ``cline --help`` and live probes, 2026-08-19):
  --auto-approve true  : auto-approve all tool calls (yolo)
  --data-dir <path>    : isolated sandbox dir (enables sandbox mode, no hub)
  -c <path>            : working directory
  -m <model-id>        : model override (format: provider/model-id)
  -P <provider>        : API provider (default: cline)
  -s <system-prompt>   : system prompt override
  --thinking <level>   : reasoning effort (none|low|medium|high|xhigh)
  --timeout <seconds>  : per-run timeout
  --retries <n>        : max consecutive retries

Note on --id: ``--id <session-id>`` forces TUI/interactive mode and CANNOT be
used in plain one-shot invocations (live-probed 2026-08-19).

MCP configuration (live-probed 2026-08-20): Each worker runs in sandbox mode
via ``--data-dir``, which places the agent core in-process (no hub daemon).
The MCP settings file is materialized at ``<data-dir>/settings/cline_mcp_settings.json``
with cao-mcp-server configured per-worker (own CAO_TERMINAL_ID + CAO_TERMINAL_TOKEN).
Credentials are seeded via a symlink allowlist from ``~/.cline/data/``.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import (
    get_provider_defaults,
    get_provider_profile_defaults,
    get_server_settings,
    resolve_provider_string_option,
)
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_cao_mcp_command
from cli_agent_orchestrator.utils.terminal import wait_for_shell
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

CLINE_BINARY = str(Path.home() / ".bun" / "bin" / "cline")

# Scratch directory for message temp files (NOT /tmp — user requirement).
SCRATCH_DIR = Path("/data/cao-scratch")

# Per-worker sandbox data directory root (D1).
CLINE_SANDBOX_ROOT = SCRATCH_DIR / "cline-home"

# User's production cline data dir (source for symlinks).
_CLINE_USER_DATA = Path.home() / ".cline" / "data"

# D2: Symlink allowlist — exactly these files are linked from the user's
# production data dir into each worker's sandbox. Everything else is fresh.
# NEVER add db/, sessions/, or locks/ (Do-NOT 4).
_SANDBOX_SYMLINK_ALLOWLIST: list[tuple[str, ...]] = [
    ("secrets.json",),
    ("globalState.json",),
    ("settings", "providers.json"),
    ("settings", "global-settings.json"),
]

# ─── Status detection ─────────────────────────────────────────────────────────
# The dispatcher loop uses `cat` as an idle sentinel:
#   - pane_current_command == "cat" → dispatcher waiting for input (IDLE/COMPLETED)
#   - pane_current_command contains "cline" → cline running (PROCESSING)
#   - pane_current_command == shell baseline → dispatcher exited (ERROR)

DISPATCHER_IDLE_CMD = "cat"

# Error patterns detected in pane output (post-completion scan).
ERROR_PATTERN = (
    r"^\s*(?:"
    r"[Ee]rror:\s+.+"
    r"|ERROR:\s+.+"
    r"|Failed to .+"
    r"|Rate limit(?:ed| exceeded)\b.*"
    r")$"
)

# Cline prints this when a run ends with finishReason "aborted" and the CLI
# process itself did not request the abort.  In the shipped bundle the text is
# the *fallback* branch of:
#     if (timedOut)       -> "aborted after timeout"
#     else if (aborted)   -> "aborted"
#     else                -> "aborted by another client"
# so the wording is a guess, not a detection — there is no other client.  What
# it does reliably mean is that the run produced no answer, so the terminal must
# not be reported COMPLETED.
ABORT_LINE = "[abort] aborted by another client"

# Keep an abort visible for two full agent-step polling periods before closing
# the run as IDLE. The provider owns this report/hold state.
_ABORT_REPORT_HOLD_S = 2.0

# F345: Default MCP connect timeout (ms) for cline workers.  The cline CLI
# uses MCP_CONNECT_TIMEOUT_MS to bound the MCP server initialization handshake.
# Under concurrent worker load, cao-mcp-server can exceed the default timeout
# (Python startup + HTTP round-trip to CAO API).  30 s is generous but bounded.
_DEFAULT_MCP_CONNECT_TIMEOUT_MS = "30000"

# F537 (#393): Per-server MCP init timeout (SECONDS) written into
# cline_mcp_settings.json. cline hard-times-out the MCP `initialize` handshake
# at its built-in 3 s default and SKIPS the server; the fix is the per-server
# "timeout" field in cline_mcp_settings.json (units: seconds). cao-mcp-server
# startup (Python import + HTTP round-trip to the CAO API) can exceed 3 s under
# concurrent worker load, so we widen it to 60 s. Overridable via providers.toml
# ([cline_cli] mcp_init_timeout_s) using the same get_provider_defaults knob the
# provider already reads for model/thinking/api_provider; floored at 30 s.
CLINE_MCP_INIT_TIMEOUT_S = 60

# Never accept a materialized timeout below this floor: below ~30 s the same
# race F537 fixes can reappear under load.
_CLINE_MCP_INIT_TIMEOUT_FLOOR_S = 30


def _resolve_cline_mcp_init_timeout_s() -> int:
    """Resolve the cline MCP init timeout (seconds) from providers.toml.

    Reads ``[cline_cli] mcp_init_timeout_s`` via the same ``get_provider_defaults``
    knob the provider already uses for other options. Falls back to
    ``CLINE_MCP_INIT_TIMEOUT_S`` when unset or invalid, and floors the result at
    ``_CLINE_MCP_INIT_TIMEOUT_FLOOR_S`` so an operator cannot re-introduce the
    F537 handshake race with too small a value.
    """
    value: int = CLINE_MCP_INIT_TIMEOUT_S
    try:
        raw = get_provider_defaults("cline_cli").get("mcp_init_timeout_s")
    except Exception:
        raw = None
    if isinstance(raw, bool):
        # bool is an int subclass; a TOML boolean is not a valid timeout.
        raw = None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    if value < _CLINE_MCP_INIT_TIMEOUT_FLOOR_S:
        value = _CLINE_MCP_INIT_TIMEOUT_FLOOR_S
    return value


def _build_dispatcher_script(
    cline_binary: str,
    msg_dir: str,
    terminal_id: str,
    base_args: str,
    env_exports: str = "",
) -> str:
    """Build the shell dispatcher loop that runs in the tmux pane.

    The loop:
    1. Waits for input via ``cat`` (the standard send_input paste delivers text
       to this blocking read).
    2. Writes the received text to a temp file.
    3. Invokes cline with the temp file content via $(cat ...).
    4. Returns to step 1 for the next message.

    Uses ``cat`` as the idle-wait command because:
    - It blocks waiting for stdin (pasted text ends with Enter + Ctrl-D)
    - tmux reports "cat" as pane_current_command when idle
    - Distinct from "cline" when processing

    The dispatcher uses Ctrl-D (EOF) on a standalone line to signal end-of-message.
    Bracketed paste from tmux delivers the text, then the provider's ``paste_enter_count``
    sends Enter (newline). The shell dispatcher sees the Enter as part of the message
    text. We use a HereDoc marker (``__CAO_MSG_END__``) as the delimiter.
    """
    # The script uses a heredoc-style delimiter: text up to a line containing
    # only __CAO_MSG_END__ is captured as the message.
    return f"""\
{env_exports}_cao_msg_n=0
while true; do
  _cao_msg_n=$((_cao_msg_n + 1))
  _cao_msgfile="{msg_dir}/{terminal_id}_${{_cao_msg_n}}.txt"
  cat > "$_cao_msgfile"
  [ -s "$_cao_msgfile" ] || continue
  {base_args} "$(cat "$_cao_msgfile")"
done
"""


class ClineCliProvider(BaseProvider):
    """Provider for Cline CLI plain-mode one-shot invocations.

    A shell dispatcher loop accepts messages via tmux paste (the standard
    send_input path), writes them to a temp file, and invokes cline.
    Status detection uses pane_current_command: ``cat`` = idle, ``cline`` =
    processing, shell baseline = error/exited.
    """

    condition_provider_key = "cline_cli"  # F611 #467

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list[str]] = None,
        skill_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._task_dispatched_flag = False
        self._agent_profile = agent_profile
        self._model = model
        self._session_id: Optional[str] = None
        self._message_count = 0
        self._pre_run_history_ids: set[str] = set()
        self._abort_reported_occ = 0
        self._abort_reported_at: float | None = None
        self._abort_retry_armed = False

    @property
    def resolved_model(self) -> Optional[str]:
        """Return the effective model resolved during command build."""
        return getattr(self, "_resolved_model", None)

    @property
    def session_id(self) -> Optional[str]:
        """Return the correlated cline session ID, if known."""
        return self._session_id

    @property
    def paste_enter_count(self) -> int:
        """After paste, one Enter sends the message, then we need EOF (Ctrl-D).

        The dispatcher loop uses ``cat > file`` which reads until EOF.
        Bracketed paste delivers the text; after paste we send Enter (so the
        last line has a newline) then the terminal_service's Enter submits.
        But cat needs EOF — we handle this via send_special_key (Ctrl-D) in
        _after_dispatch_commit_locked.

        Actually: set to 0 here. We override the submit behavior because
        `cat` needs Ctrl-D, not Enter. The base flow with paste_enter_count=0
        will paste the text without pressing Enter at all, then our
        _after_dispatch_commit_locked sends Ctrl-D to signal EOF to cat.

        UPDATE: paste_enter_count must be >= 1 for the base dispatch flow.
        We set it to 1 (one Enter after paste, which adds a trailing newline
        to the cat input). Then _after_dispatch_commit_locked sends Ctrl-D
        (EOF) to close the cat.
        """
        return 1

    @property
    def paste_submit_delay(self) -> float:
        """Short delay before Enter."""
        return 0.1

    def _pane_cmd(self) -> str:
        """Current foreground command in the worker pane ('' if unavailable)."""
        try:
            return get_backend().get_pane_current_command(self.session_name, self.window_name) or ""
        except Exception as exc:  # backend hiccup must never break delivery
            logger.debug("cline worker %s: pane command read failed: %s", self.terminal_id, exc)
            return ""

    def pre_paste_gate(self) -> None:
        """Block the paste until the dispatcher is at its ``cat`` read.

        Polls ``_pane_cmd()`` at ~0.1 s intervals for up to ~10 s.  If the
        pane remains busy (cline still running), raises
        ``TerminalInputBlockedError`` so the inbox retries delivery later
        rather than pasting into a child process where the message would be
        lost.
        """
        from cli_agent_orchestrator.models.terminal import TerminalInputBlockedError

        timeout = 10.0
        interval = 0.1
        deadline = time.time() + timeout
        while True:
            cmd = self._pane_cmd()
            if cmd in (DISPATCHER_IDLE_CMD, ""):
                return  # pane ready — allow paste
            if time.time() >= deadline:
                logger.warning(
                    "cline worker %s: pane still busy (%r) after %.1fs; "
                    "refusing paste so inbox retries later",
                    self.terminal_id,
                    cmd,
                    timeout,
                )
                raise TerminalInputBlockedError(
                    f"cline worker {self.terminal_id}: pane busy ({cmd!r}), "
                    f"paste refused after {timeout}s"
                )
            time.sleep(interval)

    def _after_dispatch_commit_locked(self) -> None:
        """After message paste + Enter, send Ctrl-D (EOF) to close the cat read."""
        self._task_dispatched_flag = True
        self._message_count += 1
        # Snapshot history IDs before cline starts (for session correlation).
        self._pre_run_history_ids = self._snapshot_history_ids()
        # Send Ctrl-D (EOF) to signal end-of-input to `cat`.
        # This must happen AFTER the paste + enter, so cat writes the file.
        import threading

        def _send_eof() -> None:
            import time as _time

            _time.sleep(0.2)  # Small delay to let Enter propagate
            # Only the dispatcher's `cat` may be given EOF.  If cline is still
            # running, this Ctrl-D would close stdin on its `run_commands`
            # child instead (e.g. an `ssh` invoked without -n), corrupting that
            # tool call.  Skip rather than damage a live run.
            pane_cmd = self._pane_cmd()
            if pane_cmd != DISPATCHER_IDLE_CMD:
                # F716 (#571): this guard fires whenever cline (node) is still
                # foreground at EOF time — including AFTER the message was
                # already delivered and pasted. Delivery state is unknown
                # here, so state the fact only; never claim non-delivery.
                logger.debug(
                    "cline worker %s: deferred EOF skipped: pane running %r",
                    self.terminal_id,
                    pane_cmd,
                )
                return
            try:
                get_backend().send_special_key(self.session_name, self.window_name, "C-d")
            except Exception as exc:
                logger.debug("Failed to send EOF to dispatcher: %s", exc)

        threading.Thread(target=_send_eof, daemon=True).start()

    def notify_status_buffer_reset(self, epoch: int) -> None:
        """Start a fresh abort-evidence epoch with the monitor byte buffer."""
        del epoch
        self._abort_reported_occ = 0
        self._abort_reported_at = None
        self._abort_retry_armed = False

    def _resolve_model(self) -> Optional[str]:
        """Resolve model: spawn override > providers.toml > profile field.

        Follows the same resolution chain as other providers: explicit model
        kwarg (from assign/handoff) > [cline_cli.profiles.<name>] model >
        [cline_cli] model > profile.model field.
        """
        if self._model:
            return self._model
        profile = None
        try:
            if self._agent_profile:
                profile = load_agent_profile(self._agent_profile)
        except (FileNotFoundError, RuntimeError) as exc:
            logger.debug(
                "Profile '%s' not loadable; falling back to providers.toml: %s",
                self._agent_profile,
                exc,
            )
        provider_defaults = get_provider_defaults("cline_cli")
        profile_name = getattr(profile, "name", None) or self._agent_profile
        profile_defaults = get_provider_profile_defaults(provider_defaults, profile_name)
        return resolve_provider_string_option(
            profile_defaults,
            provider_defaults,
            profile,
            "model",
            "model",
        )

    def _resolve_provider_id(self) -> Optional[str]:
        """Resolve the Cline API provider id (e.g. 'cline-pass').

        Resolution: providers.toml [cline_cli] api_provider > default 'cline-pass'.
        """
        provider_defaults = get_provider_defaults("cline_cli")
        return provider_defaults.get("api_provider") or "cline-pass"

    def _resolve_thinking(self) -> Optional[str]:
        """Resolve reasoning effort level for --thinking flag.

        Resolution: [cline_cli.profiles.<name>] thinking > [cline_cli] thinking >
        profile.reasoningEffort field > default 'high'.
        Valid values: none|low|medium|high|xhigh (per cline --help).

        An explicit empty string ("") in providers.toml suppresses the flag
        entirely (falls back to Cline's own provider default).
        """
        profile = None
        try:
            if self._agent_profile:
                profile = load_agent_profile(self._agent_profile)
        except (FileNotFoundError, RuntimeError):
            pass
        provider_defaults = get_provider_defaults("cline_cli")
        profile_name = getattr(profile, "name", None) or self._agent_profile
        profile_defaults = get_provider_profile_defaults(provider_defaults, profile_name)
        resolved = resolve_provider_string_option(
            profile_defaults,
            provider_defaults,
            profile,
            "thinking",
            "reasoningEffort",
        )
        if isinstance(resolved, str):
            return resolved
        return "high"

    def _data_dir(self) -> Path:
        """Return this worker's sandbox data directory path."""
        return CLINE_SANDBOX_ROOT / self.terminal_id

    def _ensure_data_dir(self) -> Path:
        """Create the worker's sandbox data dir and seed it with D2 symlinks.

        Returns the data dir path. The dir is created fresh; symlinks point at
        the user's production cline credentials so the worker is pre-authenticated.
        """
        dd = self._data_dir()
        dd.mkdir(parents=True, exist_ok=True)
        settings_dir = dd / "settings"
        settings_dir.mkdir(parents=True, exist_ok=True)

        links_created = 0
        for parts in _SANDBOX_SYMLINK_ALLOWLIST:
            link_path = dd.joinpath(*parts)
            target = _CLINE_USER_DATA.joinpath(*parts)
            if target.exists() and not link_path.exists():
                link_path.parent.mkdir(parents=True, exist_ok=True)
                link_path.symlink_to(target)
                links_created += 1

        return dd

    def _materialize_mcp_settings(self, dd: Path) -> None:
        """Write cline_mcp_settings.json into the sandbox settings dir.

        The entry uses resolve_cao_mcp_command(persisted=True) for the binary path
        and injects this worker's CAO_TERMINAL_ID + CAO_TERMINAL_TOKEN into the
        env block. A per-server ``timeout`` (seconds) is written so cline does not
        skip the server on its 3 s init default (F537 #393).
        """
        profile = None
        if self._agent_profile:
            try:
                profile = load_agent_profile(self._agent_profile)
            except (FileNotFoundError, RuntimeError):
                pass

        # Resolve command from profile or use defaults
        mcp_servers = profile.mcpServers if profile and profile.mcpServers else {}
        cao_entry = mcp_servers.get("cao-mcp-server", {})
        raw_command = cao_entry.get("command", "cao-mcp-server")
        raw_args = cao_entry.get("args", []) or []
        command, args = resolve_cao_mcp_command(raw_command, raw_args, persisted=True)

        # Build the MCP settings with per-worker identity
        env_block: dict[str, str] = {
            "CAO_TERMINAL_ID": self.terminal_id,
        }
        terminal_token = os.environ.get("CAO_TERMINAL_TOKEN", "")
        if terminal_token:
            env_block["CAO_TERMINAL_TOKEN"] = terminal_token
        # Forward endpoint so MCP server can reach the API
        from cli_agent_orchestrator.utils.http import resolve_endpoint

        env_block["CAO_ENDPOINT"] = resolve_endpoint()
        instance_id = os.environ.get("CAO_INSTANCE_ID", "")
        if instance_id:
            env_block["CAO_INSTANCE_ID"] = instance_id

        settings = {
            "mcpServers": {
                "cao-mcp-server": {
                    "command": command,
                    "args": args,
                    "env": env_block,
                    "disabled": False,
                    # F537 (#393): per-server MCP init timeout in SECONDS. Without
                    # it cline uses its built-in 3 s default and skips the server.
                    "timeout": _resolve_cline_mcp_init_timeout_s(),
                }
            }
        }

        settings_file = dd / "settings" / "cline_mcp_settings.json"
        tmp_file = settings_file.with_suffix(".json.tmp")
        tmp_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        os.replace(str(tmp_file), str(settings_file))

        logger.info(
            "cline worker %s: sandbox=%s (seeded: %d links), mcp=cao-mcp-server (resolved: %s)",
            self.terminal_id,
            dd,
            sum(1 for p in _SANDBOX_SYMLINK_ALLOWLIST if (dd / Path(*p)).is_symlink()),
            command,
        )

    def _build_env_exports(self) -> str:
        """Build shell export statements for the dispatcher pane (setdefault semantics).

        Environment variables are exported only if not already present in the
        process environment, so operator overrides are respected.
        """
        lines: list[str] = []
        # F345: Widen MCP connect timeout so cao-mcp-server has time to init
        # under concurrent worker load.
        if not os.environ.get("MCP_CONNECT_TIMEOUT_MS"):
            lines.append(f'export MCP_CONNECT_TIMEOUT_MS="{_DEFAULT_MCP_CONNECT_TIMEOUT_MS}"')
        if lines:
            return "\n".join(lines) + "\n"
        return ""

    def _build_base_args(self) -> str:
        """Build the cline base argument string (everything except the message).

        Returns a shell command fragment like:
            /home/user/.bun/bin/cline --auto-approve true -P cline-pass -m model --thinking high -s "prompt"
        """
        command_parts = [CLINE_BINARY, "--auto-approve", "true"]

        api_provider = self._resolve_provider_id()
        if api_provider:
            command_parts.extend(["-P", api_provider])

        model = self._resolve_model()
        self._resolved_model = model if (isinstance(model, str) and model) else None
        if isinstance(model, str) and model:
            command_parts.extend(["-m", model])

        thinking = self._resolve_thinking()
        if isinstance(thinking, str) and thinking:
            command_parts.extend(["--thinking", thinking])

        # System prompt from agent profile.
        profile = None
        if self._agent_profile:
            try:
                profile = load_agent_profile(self._agent_profile)
            except (FileNotFoundError, RuntimeError):
                pass

        system_prompt = profile.system_prompt if profile and profile.system_prompt else ""
        system_prompt = self._apply_skill_prompt(system_prompt)
        if system_prompt:
            command_parts.extend(["-s", system_prompt])

        # D1/D3: sandbox mode via --data-dir
        command_parts.extend(["--data-dir", str(self._data_dir())])

        return shlex.join(command_parts)

    def _msg_dir(self) -> Path:
        """Return (and create) the message scratch directory."""
        d = SCRATCH_DIR / "cline-msgs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _snapshot_history_ids(self) -> set[str]:
        """Snapshot current cline session IDs via subprocess (quiet, not in pane).

        Returns a set of session IDs from `cline history --json`.
        Uses --data-dir to read the worker's own sandbox history (D3/AC7).
        """
        try:
            result = subprocess.run(
                [CLINE_BINARY, "history", "--json", "--data-dir", str(self._data_dir())],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.debug("cline history --json failed: %s", result.stderr)
                return set()
            sessions = json.loads(result.stdout)
            return {s["sessionId"] for s in sessions if "sessionId" in s}
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as exc:
            logger.debug("Failed to snapshot cline history: %s", exc)
            return set()

    def _correlate_session_id(self) -> Optional[str]:
        """Correlate the new session ID after a cline invocation.

        Compares current history against the pre-run snapshot; the new entry
        is our session.
        """
        current_ids = self._snapshot_history_ids()
        new_ids = current_ids - self._pre_run_history_ids
        if len(new_ids) == 1:
            session_id = new_ids.pop()
            logger.debug("Correlated cline session ID: %s", session_id)
            return session_id
        elif len(new_ids) > 1:
            logger.warning("Ambiguous cline session correlation: %d new IDs found", len(new_ids))
            return None
        else:
            logger.debug("No new cline session ID found in history")
            return None

    async def initialize(self) -> bool:
        """Initialize: wait for shell, launch dispatcher loop.

        The dispatcher loop runs in the pane, accepting messages via paste
        and dispatching them to cline one-shot invocations.
        """
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        self.shell_baseline = get_backend().get_pane_current_command(
            self.session_name, self.window_name
        )

        # F329': Set up per-worker sandbox data dir and materialize MCP settings
        dd = self._ensure_data_dir()
        self._materialize_mcp_settings(dd)

        # Verify cline is available (best-effort).
        try:
            result = subprocess.run(
                [CLINE_BINARY, "--version", "--data-dir", str(self._data_dir())],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.debug("Cline CLI version: %s", result.stdout.strip())
            else:
                logger.warning("cline --version failed: %s", result.stderr)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.warning("Could not verify cline CLI: %s", exc)

        # Build the base cline args (everything except the message).
        base_args = self._build_base_args()
        msg_dir = str(self._msg_dir())

        # F345: Build env exports for the dispatcher shell (setdefault semantics).
        env_exports = self._build_env_exports()

        # Launch the dispatcher loop script in the pane.
        dispatcher = _build_dispatcher_script(
            CLINE_BINARY, msg_dir, self.terminal_id, base_args, env_exports
        )
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, dispatcher)

        # Wait for the dispatcher to reach idle (cat is blocking for input).
        # The pane_current_command should become "cat" when the dispatcher is idle.
        import asyncio

        deadline = time.time() + float(init_timeout)
        while time.time() < deadline:
            current_cmd = get_backend().get_pane_current_command(
                self.session_name, self.window_name
            )
            if current_cmd == DISPATCHER_IDLE_CMD:
                break
            await asyncio.sleep(0.5)
        else:
            raise TimeoutError(f"Cline dispatcher initialization timed out after {init_timeout}s")

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        """Detect Cline terminal state from pane command.

        Status mapping:
          - pane_current_command == "cat" → IDLE (dispatcher waiting) or
            COMPLETED (after task)
          - dispatcher idle but the tail shows ABORT_LINE → ERROR
          - pane_current_command contains "cline" → PROCESSING
          - pane_current_command == shell_baseline → ERROR (dispatcher crashed)
          - Otherwise → PROCESSING (transitioning)
        """
        native = self._resolve_native_status()
        if native is not None:
            return native

        if not self._initialized:
            return TerminalStatus.UNKNOWN

        current_cmd = self._pane_cmd()

        # Dispatcher idle: cat is waiting for input.
        if current_cmd == DISPATCHER_IDLE_CMD:
            if self._task_dispatched_flag:
                from cli_agent_orchestrator.services.pane_liveness import (
                    PANE_LIVENESS_TAIL_LINES,
                )

                lines = strip_terminal_escapes(output or "").splitlines()
                occurrences = sum(ABORT_LINE in line for line in lines)
                non_authoritative = len(lines) <= PANE_LIVENESS_TAIL_LINES
                now = time.monotonic()
                if not non_authoritative and occurrences > self._abort_reported_occ:
                    self._abort_reported_occ = occurrences
                    self._abort_reported_at = now
                    logger.warning(
                        "cline worker %s: run aborted (cline reported %r); "
                        "no answer was produced",
                        self.terminal_id,
                        ABORT_LINE,
                    )
                if self._abort_reported_at is not None:
                    if now - self._abort_reported_at < _ABORT_REPORT_HOLD_S:
                        from cli_agent_orchestrator.services.status_monitor import status_monitor

                        status_monitor.schedule_detection_retry(
                            self.terminal_id, delay_s=_ABORT_REPORT_HOLD_S
                        )
                        self._abort_retry_armed = True
                        return TerminalStatus.ERROR
                    # A reported abort closes without a reply to harvest.
                    return TerminalStatus.IDLE
                # Correlate session ID on first completion detection.
                if self._session_id is None and self._message_count > 0:
                    self._session_id = self._correlate_session_id()
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        # Dispatcher exited back to shell → error.
        if self.shell_baseline and current_cmd == self.shell_baseline:
            return TerminalStatus.ERROR

        # Cline is running (or dispatcher is transitioning).
        return TerminalStatus.PROCESSING

    def classify_injection_hazard(self, rows: list[str]) -> str | None:
        """No injection hazard in plain one-shot mode (no interactive prompts)."""
        return None

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last Cline response from captured scrollback.

        In plain mode, cline outputs the response text directly to stdout.
        The response is everything between the cline command invocation and
        the next ``cat >`` prompt from the dispatcher.
        """
        clean_output = strip_terminal_escapes(script_output)
        lines = clean_output.splitlines()

        # Find the last cline invocation line.
        cline_line_idx = None
        for idx in range(len(lines) - 1, -1, -1):
            if "cline" in lines[idx] and "--auto-approve" in lines[idx]:
                cline_line_idx = idx
                break

        if cline_line_idx is None:
            # Fallback: take the last non-empty content.
            content_lines = [l.strip() for l in lines if l.strip()]
            if content_lines:
                return "\n".join(content_lines)
            raise ValueError("No cline invocation found in output")

        # Extract content after the cline command line, up to next dispatcher prompt.
        content_lines = []
        for line in lines[cline_line_idx + 1 :]:
            stripped = line.strip()
            # Stop at the next dispatcher idle indicator.
            if "cat >" in line or line.strip().startswith("_cao_msg_n="):
                break
            if stripped:
                content_lines.append(stripped)

        result = "\n".join(content_lines).strip()
        if not result:
            raise ValueError("Empty Cline response")
        return result

    def exit_cli(self) -> str:
        """Exit the dispatcher loop. Ctrl-C followed by exit."""
        return "exit"

    def cleanup(self) -> None:
        """Clean up temp files, sandbox dir, and state."""
        self._initialized = False
        # Clean up message temp files.
        scratch = SCRATCH_DIR / "cline-msgs"
        if scratch.exists():
            for f in scratch.glob(f"{self.terminal_id}_*.txt"):
                try:
                    f.unlink()
                except OSError:
                    pass

        # D5: Remove the worker's sandbox data dir.
        # Guard: only delete if parent is CLINE_SANDBOX_ROOT and basename is our terminal_id.
        dd = self._data_dir()
        if dd.parent == CLINE_SANDBOX_ROOT and dd.name == self.terminal_id:
            if dd.exists():
                try:
                    # shutil.rmtree unlinks symlinks without following them (safe for D2 links)
                    shutil.rmtree(dd)
                    logger.info("cline worker %s: sandbox dir removed: %s", self.terminal_id, dd)
                except OSError as exc:
                    logger.warning(
                        "cline worker %s: failed to remove sandbox dir %s: %s",
                        self.terminal_id,
                        dd,
                        exc,
                    )
        else:
            logger.warning(
                "cline worker %s: sandbox dir guard refused removal: %s "
                "(parent=%s, expected=%s)",
                self.terminal_id,
                dd,
                dd.parent,
                CLINE_SANDBOX_ROOT,
            )
