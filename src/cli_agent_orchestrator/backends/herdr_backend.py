"""HerdrBackend — TerminalBackend implementation using the herdr CLI.

Herdr is a Rust-based terminal multiplexer with native agent-awareness.
This backend maps CAO operations to herdr CLI commands.

Design decisions:
- One herdr session, workspaces per CAO session (labeled cao-<name>)
- terminal_id is the stable identifier; pane_id is resolved before each operation
- Resolution cache with 5s TTL reduces redundant herdr pane list calls
- CAO_TERMINAL_ID and CAO_SESSION_NAME injected natively via ``--env`` at create
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional, cast

from cli_agent_orchestrator.backends.base import (
    LivenessVerdict,
    NativeIdentityResult,
    TerminalBackend,
    TerminalBackendError,
    TerminalNotFoundError,
)
from cli_agent_orchestrator.constants import BRACKETED_PASTE_INCOMPATIBLE_SHELLS
from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NativeFetch:
    agent_status: str | None
    status: TerminalStatus | None
    failure_cause: Literal["pane_unresolved", "command_error", "parse_error"] | None


#: Sentinel for "the pane response carried no ``agent_status`` field at all",
#: which is protocol drift and NOT herdr reporting the string "unknown" (N1).
_AGENT_STATUS_ABSENT = object()

#: Seats already warned about an unclassifiable herdr ``agent_status`` (F926
#: #778).  The FINDING counts every occurrence — that count is the signal — but
#: the log line is emitted once per seat: ``fetch_native_status`` runs on the
#: status poll, so a warning per call would bury the log it exists to inform.
#:
#: Keyed by (session, window) so two sessions reusing a window name each get
#: their own line, and bounded: a process that outlives thousands of seats
#: clears the set rather than growing it without end. Clearing costs at most one
#: repeated warning per live seat, which is the cheaper of the two mistakes.
_STATUS_UNKNOWN_WARNED: set[tuple[str, str]] = set()
_STATUS_UNKNOWN_WARNED_MAX = 1024
_STATUS_UNKNOWN_LOCK = threading.Lock()


#: ``herdr pane get`` could not be read at all (command failed, unparseable).
#: Distinct from a field that is simply not present, because "cannot ask" is not
#: an answer and callers must not treat it as a negative.
_PANE_GET_UNREADABLE = object()

#: herdr answered, and the field was not in the reply.
_PANE_GET_ABSENT = object()


def _seat_key(session_name: str, window_name: str) -> tuple[str, str]:
    """The identity of one worker seat, defined ONCE (N5).

    The warn-once set and the finding's dedupe identity must agree on what "the
    same seat" means, or the log would fall silent for a seat whose finding
    count is still climbing. Both go through here.
    """
    return (session_name, window_name)


def _terminal_id_from_window(window_name: str) -> str:
    """Recover the CAO terminal id a window name carries, or ``""``.

    ``utils.terminal.generate_window_name`` composes ``{profile}-{terminal_id}``
    and documents that the visible suffix IS the terminal id (fx155), so the
    last dash-segment is the id whenever it has the minted shape. Profiles may
    themselves contain dashes (``developer-opus``), which is why this splits on
    the LAST dash rather than the first.

    Returns ``""`` — never a guess — when the suffix is not an 8-hex id. That
    covers the legacy ``{profile}-{uuid4[:4]}`` form from the no-id branch of
    ``generate_window_name`` and any hand-made window name, and an empty string
    is exactly what a fleet-wide finding row wants: SQLite treats NULLs as
    distinct inside a UNIQUE index, so the store's dedupe contract is written in
    terms of ``""``.
    """
    from cli_agent_orchestrator.utils.terminal import is_raw_terminal_id

    suffix = window_name.rsplit("-", 1)[-1]
    return suffix if is_raw_terminal_id(suffix) else ""


def map_native_status(agent_status: str | None) -> TerminalStatus | None:
    if agent_status is None:
        return None
    return {
        "working": TerminalStatus.PROCESSING,
        "blocked": TerminalStatus.WAITING_USER_ANSWER,
        "done": TerminalStatus.COMPLETED,
        "idle": TerminalStatus.IDLE,
    }.get(agent_status)


# Herdr CLI subcommands that _run_herdr is allowed to invoke.
_HERDR_ALLOWED_SUBCOMMANDS = frozenset(
    {
        "workspace",
        "tab",
        "pane",
        "session",
        "api",
    }
)

# Pattern for safe structural argument values passed to herdr.  The goal is
# preventing argument injection (crafted --flags) under shell=False, NOT shell
# injection (which list-form subprocess already prevents).  Rejects control
# characters and NUL bytes; allows printable characters needed for filesystem
# paths, UUIDs, labels, and JSON snippets.
_SAFE_ARG_RE = re.compile(r"^[\w\-./: =,@(){}\[\]\"'\\~+#]+$", re.UNICODE)

# Flags that _run_herdr is allowed to pass to the herdr CLI.  Any argument
# starting with "--" that is not in this set is rejected to prevent argument
# injection (e.g. a crafted ``--session other`` overriding the backend's
# session selection).
_HERDR_ALLOWED_FLAGS = frozenset(
    {
        "--cwd",
        "--env",
        "--format",
        "--label",
        "--lines",
        "--pane",
        "--source",
        "--workspace",
    }
)


def _sanitize_herdr_args(args: List[str]) -> List[str]:
    """Validate herdr CLI arguments and return a shallow copy.

    Checks that all structural arguments (subcommand, flags, identifiers) are
    safe before they reach subprocess.run().  Returns a new list so static
    analysis tools see the subprocess receiving values that passed through
    this validation gate rather than the original caller-provided references.

    herdr is invoked with shell=False (list form) so shell injection is not
    possible, but argument injection (e.g. injecting ``--session other``) could
    redirect commands to unintended targets.  This sanitizer ensures that:
    1. The first positional arg is a known herdr subcommand.
    2. All structural arguments match a safe character set.
    3. Any ``--flag`` is in the allowed set (``--session`` is excluded since
       ``_run_herdr`` injects it from a trusted instance attribute).
    Terminal input payloads (the text body of ``pane send-text`` / ``pane run``)
    are exempt because they are literal content typed into a terminal pane, not
    arguments that alter herdr's own behavior.
    """
    if not args:
        raise ValueError("herdr args must not be empty")
    subcommand = args[0]
    if subcommand not in _HERDR_ALLOWED_SUBCOMMANDS:
        raise ValueError(
            f"herdr subcommand '{subcommand}' not in allowlist: "
            f"{sorted(_HERDR_ALLOWED_SUBCOMMANDS)}"
        )
    # Determine how many args are structural (subcommand + action + flags/ids).
    # ``pane send-text <pane_id> <text>`` and ``pane run <pane_id> <cmd>``
    # carry a terminal-input / shell-command payload at index 3+ that is
    # exempt from validation (it is content, not an argument that alters
    # herdr's own routing or behavior).
    if len(args) >= 2 and args[0] == "pane" and args[1] in ("send-text", "run"):
        structural_args = args[:3]
    else:
        structural_args = args
    prev_was_env = False
    for arg in structural_args:
        if not _SAFE_ARG_RE.fullmatch(arg):
            # A rejected --env value may be a secret; redact it in the error.
            shown = _redact_env_values(["--env", arg])[1] if prev_was_env else repr(arg)
            raise ValueError(f"herdr argument contains unsafe characters: {shown}")
        if arg.startswith("--") and arg not in _HERDR_ALLOWED_FLAGS:
            raise ValueError(
                f"herdr flag '{arg}' not in allowlist: " f"{sorted(_HERDR_ALLOWED_FLAGS)}"
            )
        prev_was_env = arg == "--env"
    return list(args)


def _redact_env_values(args: List[str]) -> List[str]:
    """Return a display copy of herdr args with ``--env`` values redacted.

    Operator-forwarded env values may be secrets. Any token immediately
    following ``--env`` is reduced to ``KEY=<redacted>`` (or ``<redacted>`` if
    it has no ``=``), so a create failure/timeout or sanitizer rejection never
    surfaces the raw value in an exception, log, or HTTP error detail.
    """
    redacted: List[str] = []
    prev_was_env = False
    for arg in args:
        if prev_was_env:
            key = arg.split("=", 1)[0] if "=" in arg else ""
            redacted.append(f"{key}=<redacted>" if key else "<redacted>")
            prev_was_env = False
        else:
            redacted.append(arg)
            prev_was_env = arg == "--env"
    return redacted


# Cache TTL for pane_id resolution (seconds).
# Used by get_pane_id() (fast-path, reads the cache populated at create time) and
# _resolve_workspace_id(). _resolve_pane_id_from_window() never caches pane_ids —
# it resolves the pane fresh every call.
#
# That used to be justified as "herdr renumbers panes on deletion", which
# contradicted _PANE_ID_MAP_TTL's "stable except across a full server restart"
# a few lines below. MEASURED on herdr 0.9.0 (protocol 22, grok-box-010) — four
# tabs in one workspace, then close the middle one:
#
#     BEFORE  w1:p1 tab-1   w1:p2 tab-a   w1:p3 tab-b   w1:p4 tab-c
#     close   w1:t2 (tab-a)
#     AFTER   w1:p1 tab-1                 w1:p3 tab-b   w1:p4 tab-c
#
# Surviving panes KEEP their ids and the closed id is retired, not reused
# (herdr's own skill file: "Closed tab and pane IDs are not reused"). So the
# second comment is the true one: a pane id changes only when the pane is MOVED
# to another workspace (which mints a new workspace-qualified id) or when the
# server restarts. A pane id can still go DEAD while its tab label lives, which
# is the case the inbox reconcile repairs — but a live pane's id does not shift
# under a sibling's deletion.
_PANE_CACHE_TTL = 5.0
_PROVIDER_AGENT_MARKERS = {
    "claude_code": "claude",
    "codex": "codex",
    "copilot_cli": "copilot",
    "kimi_cli": "kimi",
    "kiro_cli": "kiro",
    "opencode_cli": "opencode",
    "cursor_cli": "cursor",
    "antigravity_cli": "antigravity",
    "hermes": "hermes",
    "grok_cli": "grok",
}

# Staleness bound for the durable pane_id map (seconds). Herdr public pane_ids
# are stable except across a full server restart, so a generous TTL is a cheap
# safety net: within it, map hits are instant; after it (or on a miss) a single
# `api snapshot` refresh rebuilds the whole map, self-healing a stale entry.
_PANE_ID_MAP_TTL = 30.0


class HerdrBackend(TerminalBackend):
    supports_identity_readback = False
    """TerminalBackend implementation using herdr CLI commands.

    Maps CAO concepts to herdr:
    - CAO session → herdr workspace (labeled cao-<name>)
    - CAO terminal/window → herdr tab within workspace
    - terminal_id → stable identifier stored in CAO DB
    - pane_id → compact ID resolved via herdr pane list before each operation
    """

    def __init__(self, send_delay_ms: int = 0, herdr_session: str = "cao") -> None:
        """Initialize HerdrBackend.

        Args:
            send_delay_ms: Milliseconds to sleep between send-text and send-keys Enter.
                Configurable per-provider for bracketed paste timing.
            herdr_session: Name of the herdr session CAO operates in.
                Maps to ``herdr --session <name>``. Defaults to ``"cao"`` so CAO
                runs isolated from the user's personal herdr session.
        """
        self._send_delay_ms = send_delay_ms
        self._herdr_session = herdr_session
        # Resolution cache: terminal_id → (pane_id, timestamp)
        self._pane_cache: Dict[str, tuple[str, float]] = {}
        # Durable map: (session_name, window_name) → pane_id, rebuilt from
        # `api snapshot`. Public IDs are stable except across a full herdr
        # server restart. Keyed on the labels CAO itself writes at create time
        # (workspace label = CAO session, tab label = CAO window) because those
        # are the ONLY CAO-controlled identifiers the snapshot carries — see
        # _refresh_pane_id_map.
        self._pane_id_map: Dict[tuple[str, str], str] = {}
        # Timestamp of the last successful map rebuild; bounds map staleness
        # against a herdr restart via _PANE_ID_MAP_TTL (0.0 => never built).
        self._pane_id_map_ts: float = 0.0
        # Workspace cache: session_name → (workspace_id, timestamp)
        self._workspace_cache: Dict[str, tuple[str, float]] = {}
        self._ensure_session_running()

    @property
    def herdr_session(self) -> str:
        """The herdr session name this backend operates in."""
        return self._herdr_session

    def _run_herdr(self, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run a herdr CLI command and return the result.

        Args:
            args: Command arguments (without 'herdr' prefix)
            check: If True, raise TerminalBackendError on non-zero exit

        Returns:
            CompletedProcess result

        Raises:
            TerminalBackendError: If check=True and command fails, or if args
                contain unsafe characters or unknown subcommands.
        """
        try:
            sanitized = _sanitize_herdr_args(args)
        except ValueError as e:
            raise TerminalBackendError(f"herdr argument validation failed: {e}") from e
        cmd = ["herdr", "--session", self._herdr_session] + sanitized
        # Build a redacted display form for error messages. Two sensitive
        # sources: send-text/run payloads (terminal input) and --env values
        # (operator-forwarded, potentially secret). Never let either reach an
        # exception, log, or HTTP error detail.
        has_payload = (
            len(sanitized) >= 3 and sanitized[0] == "pane" and sanitized[1] in ("send-text", "run")
        )
        if has_payload:
            cmd_display = cmd[:6] + ["<redacted>"]
        else:
            cmd_display = _redact_env_values(cmd)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if check and result.returncode != 0:
                raise TerminalBackendError(
                    f"herdr command failed: {' '.join(cmd_display)}\n"
                    f"stderr: {result.stderr.strip()}"
                )
            return result
        except subprocess.TimeoutExpired as e:
            raise TerminalBackendError(f"herdr command timed out: {' '.join(cmd_display)}") from e
        except FileNotFoundError as e:
            raise TerminalBackendError(
                "herdr CLI not found. Install herdr to use terminal_backend='herdr'."
            ) from e

    def _parse_herdr_json(self, stdout: str) -> dict:
        """Parse herdr CLI JSON output, handling the envelope format.

        Herdr wraps responses in {"id":..., "result": {...}} envelopes.
        """
        data = json.loads(stdout)
        if isinstance(data, dict) and "result" in data:
            return cast(dict, data["result"])
        return cast(dict, data)

    @staticmethod
    def _foreground_processes(parsed: dict[str, object]) -> list[dict[str, object]]:
        """Extract ``foreground_processes`` from a parsed ``pane process-info`` body.

        F880 (#733): herdr 0.9.0 (protocol 22) nests the list under
        ``result.process_info.foreground_processes`` — after ``_parse_herdr_json``
        strips the ``result`` envelope, that is ``parsed["process_info"]
        ["foreground_processes"]``. The former readers looked for a top-level or
        ``pane.foreground_processes`` key (the 0.7.x shape) and so ALWAYS got
        ``None`` on 0.9.0 — which is why ``get_pane_current_command`` returned
        ``None`` and the launch-health probe read an empty seat and raised
        ``ProviderLaunchFailed`` for a live provider (observed live on
        grok-box-009: process-info reported ``[{"name":"grok",...}]`` while the
        old path yielded nothing). Tolerates both shapes so a schema that moves
        it back to the top level still resolves. Returns [] on anything unexpected.
        """
        if not isinstance(parsed, dict):
            return []
        info = parsed.get("process_info")
        if isinstance(info, dict) and isinstance(info.get("foreground_processes"), list):
            return [p for p in info["foreground_processes"] if isinstance(p, dict)]
        # Fallbacks for other/older shapes (top-level or under "pane").
        for candidate in (
            parsed,
            parsed.get("pane") if isinstance(parsed.get("pane"), dict) else None,
        ):
            if isinstance(candidate, dict) and isinstance(
                candidate.get("foreground_processes"), list
            ):
                return [p for p in candidate["foreground_processes"] if isinstance(p, dict)]
        return []

    def _resolve_workspace_id(self, session_name: str) -> str:
        """Resolve session_name (workspace label) to workspace ID.

        Uses _workspace_cache with the same TTL as pane cache.

        Args:
            session_name: CAO session name (used as workspace label)

        Returns:
            Workspace ID

        Raises:
            TerminalBackendError: If workspace not found
        """
        # Check cache
        if session_name in self._workspace_cache:
            workspace_id, cached_at = self._workspace_cache[session_name]
            if time.time() - cached_at < _PANE_CACHE_TTL:
                return workspace_id

        result = self._run_herdr(["workspace", "list"])
        try:
            data = self._parse_herdr_json(result.stdout)
            workspaces = data.get("workspaces", []) if isinstance(data, dict) else data
        except json.JSONDecodeError as e:
            raise TerminalBackendError(f"Failed to parse herdr workspace list output: {e}") from e

        for ws in workspaces:
            if ws.get("label") == session_name:
                ws_id = str(ws["workspace_id"])
                self._workspace_cache[session_name] = (ws_id, time.time())
                try:
                    from cli_agent_orchestrator.clients.database import record_workspace_mapping

                    record_workspace_mapping(ws_id, session_name)
                except Exception:
                    logger.exception(
                        "herdr_workspace_map_backfill_failed workspace=%s session=%s",
                        ws_id,
                        session_name,
                    )
                return ws_id

        raise TerminalBackendError(f"Workspace with label '{session_name}' not found")

    # --- Session lifecycle ---

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        terminal_token: Optional[str] = None,
        allowed_blocked_values: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create a herdr workspace (= CAO session) with an initial tab."""
        import os

        working_directory = working_directory or os.getcwd()

        args = ["workspace", "create", "--label", session_name]
        if working_directory:
            args.extend(["--cwd", working_directory])
        # Inject CAO identity + operator-forwarded env natively via --env
        # (replaces the former shell ``export`` send-text injection).
        args.extend(
            self._build_env_args(
                terminal_id,
                session_name,
                extra_env,
                terminal_token=terminal_token,
                allowed_blocked_values=allowed_blocked_values,
            )
        )

        result = self._run_herdr(args)

        # Parse workspace ID and root tab_id from output for cache
        workspace_id = ""
        root_tab_id = ""
        try:
            ws_data = self._parse_herdr_json(result.stdout)
            root_pane = ws_data.get("root_pane", {})
            workspace_id = str(root_pane.get("workspace_id", ""))
            root_tab_id = str(root_pane.get("tab_id", ""))
            if workspace_id:
                self._workspace_cache[session_name] = (workspace_id, time.time())
                try:
                    from cli_agent_orchestrator.clients.database import record_workspace_mapping

                    record_workspace_mapping(workspace_id, session_name)
                except Exception:
                    logger.exception(
                        "herdr_workspace_map_write_failed workspace=%s session=%s",
                        workspace_id,
                        session_name,
                    )
        except (json.JSONDecodeError, KeyError):
            pass  # Non-fatal; we can resolve later

        # Parse root pane_id from the create response to seed the pane cache.
        new_pane_id = self._parse_new_pane_id(result.stdout)

        # Label the root tab so it shows the CAO window name in herdr TUI.
        if root_tab_id:
            self._run_herdr(["tab", "rename", root_tab_id, window_name], check=False)

        # Seed the pane cache so get_pane_id() keeps its fast path (formerly
        # seeded via the send-text env path). R4 will replace this cache with a
        # snapshot map.
        if new_pane_id:
            self._pane_cache[terminal_id] = (new_pane_id, time.time())

        logger.info(f"Created herdr workspace: {session_name} in {working_directory}")
        return window_name

    def session_exists(self, session_name: str) -> bool:
        """Check if a workspace with the given label exists."""
        result = self._run_herdr(["workspace", "list"], check=False)
        if result.returncode != 0:
            return False
        try:
            data = self._parse_herdr_json(result.stdout)
            workspaces = data.get("workspaces", []) if isinstance(data, dict) else data
            return any(ws.get("label") == session_name for ws in workspaces)
        except (json.JSONDecodeError, KeyError):
            return False

    def list_sessions(self) -> List[Dict[str, str]]:
        """List all herdr workspaces as sessions."""
        result = self._run_herdr(["workspace", "list"], check=False)
        if result.returncode != 0:
            return []
        try:
            data = self._parse_herdr_json(result.stdout)
            workspaces = data.get("workspaces", []) if isinstance(data, dict) else data
            return [
                {
                    "id": ws.get("label", str(ws.get("workspace_id", ""))),
                    "name": ws.get("label", str(ws.get("workspace_id", ""))),
                    "status": "active",
                }
                for ws in workspaces
            ]
        except (json.JSONDecodeError, KeyError):
            return []

    def kill_session(self, session_name: str) -> bool:
        """Close a herdr workspace by workspace_id (herdr only accepts id, not --label)."""
        try:
            workspace_id = self._resolve_workspace_id(session_name)
        except TerminalBackendError:
            logger.warning(f"kill_session: workspace '{session_name}' not found")
            return False
        intent = None
        try:
            from cli_agent_orchestrator.clients.database import begin_teardown_intent

            intent = begin_teardown_intent(workspace_id, session_name)
        except Exception:
            logger.exception(
                "herdr_teardown_intent_begin_failed workspace=%s session=%s",
                workspace_id,
                session_name,
            )
        try:
            result = self._run_herdr(["workspace", "close", workspace_id], check=False)
        except Exception:
            if intent is not None:
                try:
                    from cli_agent_orchestrator.clients.database import settle_teardown_intent

                    settle_teardown_intent(
                        workspace_id,
                        intent["generation"],
                        issued=False,
                    )
                except Exception:
                    logger.exception(
                        "herdr_teardown_intent_void_failed workspace=%s generation=%s",
                        workspace_id,
                        intent["generation"],
                    )
            raise
        if intent is not None:
            try:
                from cli_agent_orchestrator.clients.database import settle_teardown_intent

                settle_teardown_intent(
                    workspace_id,
                    intent["generation"],
                    issued=result.returncode == 0,
                )
            except Exception:
                logger.exception(
                    "herdr_teardown_intent_settle_failed workspace=%s generation=%s",
                    workspace_id,
                    intent["generation"],
                )
        if result.returncode == 0:
            self._workspace_cache.pop(session_name, None)
            logger.info(f"Killed herdr workspace: {session_name}")
            return True
        return False

    # --- Window/tab lifecycle ---

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        window_shell: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        terminal_token: Optional[str] = None,
        allowed_blocked_values: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create a new tab in the workspace."""
        import os

        working_directory = working_directory or os.getcwd()

        # Resolve workspace ID
        workspace_id = self._resolve_workspace_id(session_name)

        args = ["tab", "create", "--workspace", workspace_id, "--label", window_name]
        if working_directory:
            args.extend(["--cwd", working_directory])
        # Inject CAO identity + operator-forwarded env natively via --env
        # (replaces the former shell ``export`` send-text injection).
        args.extend(
            self._build_env_args(
                terminal_id,
                session_name,
                extra_env,
                terminal_token=terminal_token,
                allowed_blocked_values=allowed_blocked_values,
            )
        )

        result = self._run_herdr(args)

        # Parse the new pane_id directly from the create response
        new_pane_id = self._parse_new_pane_id(result.stdout)

        # Seed the pane cache so get_pane_id() keeps its fast path (formerly
        # seeded via the send-text env path). R4 will replace this cache with a
        # snapshot map.
        if new_pane_id:
            self._pane_cache[terminal_id] = (new_pane_id, time.time())

        if window_shell is not None and new_pane_id is not None:
            # Wait for shell startup before sending the initial command.
            time.sleep(0.5)
            try:
                self._run_herdr(["pane", "run", new_pane_id, window_shell])
            except TerminalBackendError as e:
                logger.warning(f"create_window: pane run failed for {new_pane_id} (non-fatal): {e}")

        logger.info(f"Created herdr tab in workspace {session_name}")
        return window_name

    def window_liveness(self, session_name: str, window_name: str) -> str:
        """Classify a window as ``live`` / ``gone`` / ``error``.

        Same family as F893 (#745) and F900 (#752): this is a port method herdr
        never implemented, so it inherited ``base.py``'s fail-closed default of
        ``"error"`` — and callers read ``"error"`` as "might still be alive".
        ``_acquire_resume_leases`` (terminal_service.py) treats any prior owner of
        the resume uuid whose window reads ``live`` OR ``error`` as a conflict, so
        under herdr EVERY resume of a hibernated terminal failed
        ``500 owner_conflict`` (observed live on grok-box-006: H4 hibernate
        succeeded, the resume that should have followed it did not).

        Resolution mirrors ``_resolve_pane_id_from_window`` but keeps the two
        failure kinds apart, which is the whole point of the three-valued answer:

        - the workspace label is absent            -> ``"gone"``
        - the workspace is there but has no such tab -> ``"gone"``
        - herdr itself could not answer (CLI/socket/parse failure) -> ``"error"``
        - the tab is present                        -> ``"live"``

        ``_resolve_workspace_id`` caches its answer, so a workspace that has just
        been closed is re-read rather than trusted from cache when the lookup
        misses; the cache only ever holds resolved ids.
        """
        try:
            result = self._run_herdr(["workspace", "list"], check=False)
            if result.returncode != 0:
                return "error"
            data = self._parse_herdr_json(result.stdout)
            workspaces = data.get("workspaces", []) if isinstance(data, dict) else data
        except (json.JSONDecodeError, TerminalBackendError, AttributeError, TypeError):
            return "error"
        workspace_id = None
        for workspace in workspaces or []:
            if isinstance(workspace, dict) and workspace.get("label") == session_name:
                workspace_id = str(workspace.get("workspace_id", ""))
                break
        if not workspace_id:
            # The workspace itself is gone — so is every window in it.
            return "gone"

        try:
            result = self._run_herdr(["tab", "list"], check=False)
            if result.returncode != 0:
                return "error"
            data = self._parse_herdr_json(result.stdout)
            tabs = data.get("tabs", []) if isinstance(data, dict) else data
        except (json.JSONDecodeError, TerminalBackendError, AttributeError, TypeError):
            return "error"
        for tab in tabs or []:
            if not isinstance(tab, dict):
                continue
            if tab.get("workspace_id") == workspace_id and tab.get("label") == window_name:
                return "live"
        return "gone"

    def _tabs_in_workspace(self, workspace_id: str) -> list[str]:
        """Return the tab_ids currently in ``workspace_id`` (F881 #734).

        Used by ``kill_window`` to decide tab-close vs workspace-close. Returns
        an empty list on any lookup/parse failure — the caller treats "cannot
        tell" as "do not assume this is the last tab", so it never collapses a
        workspace it failed to enumerate.
        """
        try:
            result = self._run_herdr(["tab", "list"], check=False)
            if result.returncode != 0:
                return []
            data = self._parse_herdr_json(result.stdout)
            tabs = data.get("tabs", []) if isinstance(data, dict) else data
        except (json.JSONDecodeError, TerminalBackendError, AttributeError, TypeError):
            return []
        out: list[str] = []
        for tab in tabs:
            if isinstance(tab, dict) and tab.get("workspace_id") == workspace_id:
                tid = tab.get("tab_id")
                if tid is not None:
                    out.append(str(tid))
        return out

    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Close a single terminal's TAB, collapsing the workspace only when it
        is the last tab (F881 #734, decision B).

        herdr topology is one workspace per CAO session and one tab per CAO
        terminal. The former implementation closed the terminal's *pane*
        (``pane close``); for the workspace's root pane that collapses the ENTIRE
        workspace, taking every sibling tab with it — the A2 defect where a
        root-terminal launch failure 404'd later ``POST /sessions/{s}/terminals``.

        tmux parity is "the session survives while any window exists". So:
        - resolve this window's tab and enumerate the workspace's tabs;
        - if OTHER tabs remain, ``tab close <tab_id>`` — the workspace and its
          siblings stay alive;
        - if this is the LAST tab (or the workspace can no longer be enumerated
          and only this pane resolves), ``workspace close`` so the empty
          workspace is torn down exactly once.

        Explicit whole-session teardown still goes through ``kill_session``
        (``workspace close``) unchanged; this method only governs per-terminal
        teardown.
        """
        # Resolve the workspace and this window's tab. If either cannot be
        # resolved the terminal/tab is already gone — nothing to close.
        try:
            workspace_id = self._resolve_workspace_id(session_name)
            tab_id = self._resolve_tab_id(session_name, workspace_id, window_name)
        except TerminalBackendError:
            logger.warning(f"kill_window: could not resolve tab for {session_name}:{window_name}")
            return False

        tab_ids = self._tabs_in_workspace(workspace_id)
        other_tabs = [tid for tid in tab_ids if tid != tab_id]

        # Collapse the workspace ONLY when enumeration positively shows this is
        # the sole remaining tab. If enumeration failed/returned empty we cannot
        # prove there are no siblings, so we close only this tab and leave the
        # workspace — leaking an empty workspace is recoverable; collapsing one
        # that still holds an unseen sibling is the A2 regression this fixes.
        is_confirmed_last_tab = tab_ids == [tab_id]

        if not is_confirmed_last_tab:
            # Siblings remain (or the sibling set is unknown) — close only this
            # tab, never the workspace.
            result = self._run_herdr(["tab", "close", tab_id], check=False)
            if result.returncode == 0:
                logger.info(
                    "Closed herdr tab %s for %s:%s (workspace %s kept; enumerated tabs=%s)",
                    tab_id,
                    session_name,
                    window_name,
                    workspace_id,
                    tab_ids or "unknown",
                )
                return True
            logger.warning(
                "kill_window: tab close %s failed (rc=%s) for %s:%s",
                tab_id,
                result.returncode,
                session_name,
                window_name,
            )
            return False

        # Confirmed last tab — close the workspace exactly once so the now-empty
        # session is torn down.
        result = self._run_herdr(["workspace", "close", workspace_id], check=False)
        if result.returncode == 0:
            logger.info(
                "Closed herdr workspace %s for %s:%s (last tab %s)",
                workspace_id,
                session_name,
                window_name,
                tab_id,
            )
            # Drop the workspace cache so a later create_session re-resolves.
            self._workspace_cache.pop(session_name, None)
            return True
        logger.warning(
            "kill_window: workspace close %s failed (rc=%s) for %s:%s",
            workspace_id,
            result.returncode,
            session_name,
            window_name,
        )
        return False

    # --- Input ---

    def _pane_is_bracketed_paste_incompatible(self, session_name: str, window_name: str) -> bool:
        """Whether the pane's live foreground command is a known shell.

        Mirrors ``TmuxClient._pane_is_bracketed_paste_incompatible`` (clients/
        tmux.py) -- see that method's own docstring for the failure mode this
        guards against. Fails closed to "compatible" (returns False) on any
        lookup failure or unrecognized command name.
        """
        command = self.get_pane_current_command(session_name, window_name)
        return command is not None and command in BRACKETED_PASTE_INCOMPATIBLE_SHELLS

    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
    ) -> None:
        """Send text to a pane via herdr pane send-text + send-keys Enter.

        When force_bracketed_paste=True, wraps content in \\x1b[200~...\\x1b[201~
        so Claude Code's Ink TUI treats it as a paste event rather than raw
        keystrokes. Without this, multi-line prompts go into multi-line mode
        and the final Enter adds a newline instead of submitting.

        ``submit_delay`` is accepted for parity with the backend interface; herdr
        governs its own post-paste timing below (the generous 2s bracketed wait
        already covers Claude Code's Ink renderer), so the value is not used here.
        """
        # Resolve pane_id from terminal_id stored in DB metadata
        # The window_name is used as a lookup key in CAO's DB → terminal_id mapping
        # For herdr, we need the terminal_id. The service layer passes session:window
        # which maps to a terminal in the DB. We'll resolve via the pane list.
        pane_id = self._resolve_pane_id_from_window(session_name, window_name)

        # Wrap in bracketed paste sequences when requested -- UNLESS the pane's
        # live foreground process is a known shell (see
        # BRACKETED_PASTE_INCOMPATIBLE_SHELLS' own docstring in constants.py):
        # a bare shell doesn't understand the escape sequences and glues them
        # onto the first token of whatever's sent, corrupting it. Same
        # tmux-backend fix (clients/tmux.py's
        # _pane_is_bracketed_paste_incompatible), mirrored here since herdr's
        # ``pane send-text`` writes raw bytes to the pty just like tmux's
        # paste-buffer -- the same corruption is equally possible here, and
        # herdr already exposes the same get_pane_current_command primitive.
        # Fails closed to "compatible" (wraps, existing behavior) on a lookup
        # failure or unrecognized command name. Only probed when
        # force_bracketed_paste is actually requested -- an extra herdr
        # round-trip whose result would otherwise be discarded.
        if force_bracketed_paste and not self._pane_is_bracketed_paste_incompatible(
            session_name, window_name
        ):
            text = "\x1b[200~" + keys + "\x1b[201~"
        else:
            text = keys

        self._run_herdr(["pane", "send-text", pane_id, text])

        # Allow the TUI to process the pasted content before sending Enter.
        # For bracketed paste, the TUI needs time to process the end sequence
        # and enter multi-line mode; 2s is intentionally generous.
        # For non-bracketed paste, use the configurable send_delay_ms.
        if force_bracketed_paste:
            time.sleep(2.0)
        elif self._send_delay_ms > 0:
            time.sleep(self._send_delay_ms / 1000.0)

        # Send Enter key(s)
        for _ in range(enter_count):
            self._run_herdr(["pane", "send-keys", pane_id, "Enter"])

    def send_special_key(self, session_name: str, window_name: str, key: str) -> None:
        """Send a special key to a pane."""
        pane_id = self._resolve_pane_id_from_window(session_name, window_name)

        # Map key names
        if not key or key.lower() == "enter":
            self._run_herdr(["pane", "send-keys", pane_id, "Enter"])
        elif key == "C-c":
            self._run_herdr(["pane", "send-keys", pane_id, "C-c"])
        elif key == "C-d":
            self._run_herdr(["pane", "send-keys", pane_id, "C-d"])
        else:
            # Pass key name directly
            self._run_herdr(["pane", "send-keys", pane_id, key])

    # --- Output ---

    def get_history(
        self,
        session_name: str,
        window_name: str,
        tail_lines: Optional[int] = None,
        strip_escapes: bool = False,
        full_history: bool = False,
        visible_only: bool = False,
    ) -> str:
        """Read pane output via herdr pane read."""
        pane_id = self._resolve_pane_id_from_window(session_name, window_name)

        args = ["pane", "read", pane_id]
        if visible_only:
            # herdr has no viewport/scrollback split; approximate the "current
            # screen" contract with a small bounded recent read. In practice this
            # arm is unreachable from the one visible_only caller (the stale-
            # PROCESSING capture fallback): event-inbox backends return from
            # get_status() before that fallback is reached.
            args.extend(["--source", "recent", "--lines", "50"])
        elif full_history:
            pass  # no flags — returns full scrollback
        elif tail_lines:
            args.extend(["--source", "recent", "--lines", str(tail_lines)])
        else:
            args.extend(["--source", "recent", "--lines", "500"])
        # F900 (#752): honour strip_escapes in BOTH directions. The contract is
        # symmetric — TmuxClient.get_history adds capture-pane ``-e`` exactly when
        # strip_escapes is False, i.e. False means "give me the escapes". This used
        # to set ``--format text`` only for True and otherwise leave the format
        # unset; herdr's default for ``pane read`` is ALREADY escape-stripped
        # (measured on herdr 0.9.0, grok-box-006: default and ``--format text``
        # both yield 0 escape sequences, ``--format ansi`` yields the SGR run),
        # so strip_escapes=False silently returned plain text on herdr and never
        # on tmux. draft_guard._read_provider_draft asks for escapes on behalf of
        # a provider with composer_parse_accepts_escapes (codex, for dim-SGR ghost
        # text); it got none, its parser returned None, and every codex worker
        # died "Composer state is unreadable" on its first delivery.
        args.extend(["--format", "text" if strip_escapes else "ansi"])

        result = self._run_herdr(args, check=False)
        if result.returncode != 0:
            logger.warning(f"herdr pane read failed: {result.stderr}")
            return ""
        return cast(str, result.stdout)

    def capture_viewport(self, session_name: str, window_name: str) -> str:
        """Capture only the current pane viewport as escape-normalized text.

        Contract mirrored from ``clients/tmux.py``'s ``capture-pane -p``, whose
        three properties are (a) viewport only, (b) no scrollback, (c) escapes
        stripped. herdr supplies all three natively:

        - ``--source visible`` is the currently rendered viewport, so no
          scrollback rows are included (verified against the installed binary's
          own ``herdr pane --help``, which lists
          ``--source visible|recent|recent-unwrapped``);
        - ``--format text`` strips ANSI, the same mechanism ``get_history``
          already relies on for ``strip_escapes=True``.

        Without this override the class inherits ``base.py``'s fail-closed
        ``NotImplementedError``, which the pre-open safety probe turns into
        ``probe_failure=empty_capture`` and every inbox delivery is vetoed
        with ``safety_unverified`` (F674 #529).

        A failed read still fails closed: returning ``""`` keeps the probe's
        empty-capture veto rather than admitting an unverified injection.
        """
        pane_id = self._resolve_pane_id_from_window(session_name, window_name)

        result = self._run_herdr(
            ["pane", "read", pane_id, "--source", "visible", "--format", "text"],
            check=False,
        )
        if result.returncode != 0:
            logger.warning(f"herdr capture_viewport failed: {result.stderr}")
            return ""
        return cast(str, result.stdout)

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        """Get pane CWD via herdr pane get."""
        pane_id = self._resolve_pane_id_from_window(session_name, window_name)

        result = self._run_herdr(["pane", "get", pane_id], check=False)
        if result.returncode != 0:
            return None
        try:
            data = self._parse_herdr_json(result.stdout)
            # pane get returns {"pane": {...}} inside result
            pane_info = data.get("pane", data) if isinstance(data, dict) else data
            return cast(Optional[str], pane_info.get("cwd"))
        except (json.JSONDecodeError, AttributeError):
            return None

    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        """Get the pane's live foreground process name via ``herdr pane
        process-info``.

        NOT ``herdr pane get``: that command's ``foreground_process`` field
        is null/absent across all pane states on herdr 0.7.5 (confirmed
        live against a running herdr server), so this callable would always
        return ``None`` and every caller that branches on it (this class's
        own ``_pane_is_bracketed_paste_incompatible``, plus
        ``codex``/``kiro_cli``'s ``shell_baseline`` TUI-exit detection) would
        silently never fire on herdr. ``pane process-info`` instead reports
        real process names (``"bash"``, ``"claude"``, etc.) via
        ``foreground_processes``.
        """
        pane_id = self._resolve_pane_id_from_window(session_name, window_name)

        result = self._run_herdr(["pane", "process-info", "--pane", pane_id], check=False)
        if result.returncode != 0:
            return None
        try:
            data = self._parse_herdr_json(result.stdout)
            processes = self._foreground_processes(data)
            if not processes:
                return None
            return cast(Optional[str], processes[0].get("name"))
        except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
            return None

    def read_native_identity(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        expected_provider: str,
    ) -> NativeIdentityResult:
        """Read the cached event agent and corroborate its current pane route."""
        from cli_agent_orchestrator.services.herdr_inbox_registry import (
            get_herdr_inbox_service,
        )

        service = get_herdr_inbox_service()
        expected_agent = _PROVIDER_AGENT_MARKERS.get(expected_provider)
        if service is None or expected_agent is None:
            return NativeIdentityResult(None, None, "unavailable")
        try:
            resolved_pane = self._resolve_pane_id_from_window(session_name, window_name)
        except TerminalBackendError:
            return NativeIdentityResult(None, None, "unavailable")
        result = self._run_herdr(["pane", "get", resolved_pane], check=False)
        foreground_process: str | None = None
        if result.returncode == 0:
            try:
                data = self._parse_herdr_json(result.stdout)
                pane_info = data.get("pane", data) if isinstance(data, dict) else data
                foreground_process = cast(str | None, pane_info.get("foreground_process"))
            except (json.JSONDecodeError, AttributeError):
                from cli_agent_orchestrator.utils.tombstones import tombstone

                tombstone("TS-0006")
                foreground_process = None
        marker = service.read_identity_marker(terminal_id)
        if marker is None or marker.pane_id != resolved_pane:
            return NativeIdentityResult(None, foreground_process, "unavailable")
        verdict: Literal["match", "mismatch"] = (
            "match" if marker.agent == expected_agent else "mismatch"
        )
        return NativeIdentityResult(marker.agent, foreground_process, verdict)

    # --- Attach ---

    def attach_session(self, session_name: str) -> None:
        """Attach the user's terminal to the herdr UI, focused on the CAO workspace.

        Strategy:
        1. Focus the CAO workspace in the running herdr server (so the UI opens
           on the right workspace).
        2. Exec `herdr` to replace the current process with the full herdr TUI.

        This mirrors how `tmux attach-session -t <session>` works — it opens
        the multiplexer UI showing the requested session.
        """
        import os

        workspace_id = self._resolve_workspace_id(session_name)

        # Focus the workspace so herdr opens on it when we attach
        self._run_herdr(["workspace", "focus", workspace_id], check=False)

        # Replace current process with herdr TUI, targeting the CAO session.
        # Equivalent to `tmux attach-session -t <session>`.
        os.execvp("herdr", ["herdr", "--session", self._herdr_session])

    def prepare_web_attach(self, session_name: str, window_name: str) -> List[str]:
        """Focus the requested Herdr tab and return the browser PTY attach command."""
        workspace_id = self._resolve_workspace_id(session_name)
        tab_id = self._resolve_tab_id(session_name, workspace_id, window_name)
        self._run_herdr(["tab", "focus", tab_id])
        return ["herdr", "--session", self._herdr_session]

    # --- Capability overrides ---

    def supports_event_inbox(self) -> bool:
        """Herdr uses socket events for inbox delivery."""
        return True

    def fetch_native_status(self, session_name: str, window_name: str) -> NativeFetch:
        """Query herdr's native agent_status for a pane.

        Uses herdr pane get to read the agent_status field directly, avoiding
        pane content parsing entirely when herdr knows the agent state.

        Mapping (all five herdr agent states):
        - working  -> PROCESSING
        - blocked  -> WAITING_USER_ANSWER
        - done     -> COMPLETED
        - idle     -> IDLE  (caller disambiguates IDLE vs COMPLETED via _task_dispatched)
        - unknown  -> None  (herdr has no agent registered for the pane)

        "unknown" maps to None (not ERROR) because a wrapped launch command
        (e.g. ``podman exec`` / ``docker exec``) makes herdr's foreground
        process the wrapper, not the nested agent CLI, so herdr never registers
        the agent and reports "unknown" indefinitely. A provider that launches
        that way declares ``launch_hides_agent_from_backend`` (providers/base.py)
        so F935's launch-health gate stands down for it instead of reading this
        same condition as a dead seat. None signals
        "unknown/unresolvable at the backend level" and lets the caller resolve
        status another way rather than flagging a healthy pane as ERROR.

        Returns None on backend errors (command failure, parse error) and for
        an "unknown"/unrecognized agent_status.
        """
        try:
            pane_id = self._resolve_pane_id_from_window(session_name, window_name)
        except TerminalBackendError:
            return NativeFetch(None, None, "pane_unresolved")

        # N5: the ONE reader. `probe_agent_detected` projects `agent` from the
        # same mapping, so the two cannot drift on how a pane reply is parsed.
        pane_info = self._pane_get(pane_id)
        if pane_info is _PANE_GET_UNREADABLE:
            return NativeFetch(None, None, "command_error")

        try:
            pane_info = cast(Dict[str, object], pane_info)
            agent_status = pane_info.get("agent_status", _AGENT_STATUS_ABSENT)
            # F926 (#778) detection half: herdr names the agent it RECOGNISED in
            # this pane. Its ABSENCE is what separates "no provider process is
            # alive here" from "the provider is there, not yet classified" —
            # see _record_status_unknown for the measurements.
            detected_agent = pane_info.get("agent")
        except (json.JSONDecodeError, AttributeError, TypeError):
            return NativeFetch(None, None, "parse_error")
        # N5: `probe_agent_detected` projects `agent` from the same `_pane_get`
        # mapping this projects `agent_status` from, so the two cannot disagree
        # about how a pane reply parses or what "absent" means.
        absent = agent_status is _AGENT_STATUS_ABSENT
        if not absent and not isinstance(agent_status, str):
            # A PRESENT field of the wrong type really is a parse error.
            return NativeFetch(None, None, "parse_error")
        # N1: a pane response that omits ``agent_status`` entirely is protocol
        # drift, not herdr answering "unknown" — the finding says which, so the
        # diagnostic added to distinguish these conditions actually does.
        #
        # It is deliberately NOT reported as ``parse_error``: a failure_cause
        # becomes ``probe_failure`` in the probe meta and inbox_service treats
        # that key as a delivery VETO, so typing a missing field would stop
        # every message to that seat the moment herdr renamed a JSON key. The
        # fallback to pane scraping is the right behaviour either way; only the
        # explanation differs.
        reported: str | None = None if absent else cast(str, agent_status)
        status = None if absent else map_native_status(reported)
        if status is None:
            self._record_status_unknown(
                session_name,
                window_name,
                pane_id,
                reported,
                absent=absent,
                detected_agent=detected_agent if isinstance(detected_agent, str) else None,
            )
        return NativeFetch(reported, status, None)

    @staticmethod
    def _record_status_unknown(
        session_name: str,
        window_name: str,
        pane_id: str,
        agent_status: str | None,
        *,
        absent: bool = False,
        detected_agent: str | None = None,
    ) -> None:
        """F926 (#778): count the silent fall-back to pane scraping.

        herdr answered for the pane but CAO has no native status for it, so the
        caller quietly reverts to tmux-style scraping. That fallback is correct
        — see :meth:`fetch_native_status` on why this must NOT become a
        ``failure_cause`` (a ``probe_failure`` in the probe meta is a delivery
        VETO at ``inbox_service``'s safety gate, so typing it would stop every
        message to exactly the seats this is about) — but being correct is not
        the same as being visible.

        Measured cause on herdr 0.9.0 (protocol 22): herdr's own bundled
        agent-detection manifests are uneven. ``pi.toml`` carries a single rule
        whose state is ``working``; ``cline.toml`` carries only ``working`` and
        ``blocked``; ``codex.toml`` carries ``idle`` rules and so resolves. A
        pi/cline pane sitting at its prompt therefore matches no rule and herdr
        reports ``unknown`` forever, which is why the cheap lanes carry no
        native truth while codex does. The gap is herdr's, not CAO's, so what
        CAO owes is a counted row instead of silence.

        ``absent`` separates herdr SAYING ``unknown`` from the pane response
        carrying no ``agent_status`` field at all (protocol drift).

        ``detected_agent`` separates the two conditions that actually matter
        operationally, and they are not the same problem. Measured on herdr
        0.9.0 by polling three panes once a second from the moment the command
        was sent:

            live pane   None/unknown (0s) -> pi/unknown (1s) -> pi/idle (4s)
            crashed     None/unknown, agent field NEVER present, indefinitely
            bare shell  None/unknown, agent field NEVER present, indefinitely

        An ``unknown`` WITH an agent named is a classification gap and is
        transient — herdr holds the process and is still deciding. An
        ``unknown`` with NO agent named means herdr recognised no provider
        process in the pane at all: it crashed, never started, or already
        exited. That one is permanent, and it is a LIVENESS fact wearing a
        status field's clothes, which is why it is named in the row rather than
        counted as one more unclassifiable seat.

        It is NOT a launch-shape problem, and the row should not send anyone
        hunting for one. Five shapes were measured — ``exec`` with an absolute
        path, absolute path without ``exec`` (CAO's actual shape, since
        ``create_window`` passes no ``window_shell`` and the provider is typed
        in afterwards), a bare name on PATH, a ``bash -lc`` wrapper, and with
        and without arguments — and herdr detected the agent in every one, even
        though the foreground process reads as ``bun``. herdr keys on the
        basename of argv[0], not the foreground command name.

        Deduplicated per seat: the row's ``count`` is how often this seat fell
        back, and its ``dedupe_key`` is which seat did. ``terminal_id`` is the
        window name's own suffix — ``generate_window_name`` makes the visible
        suffix the terminal id — so the row joins to the event and state tables
        instead of leaving a blank column in ``cao diag findings``. Never raises:
        the wiring seam swallows, and a missing runtime is simply silent.
        """
        terminal_id = _terminal_id_from_window(window_name)
        try:
            from cli_agent_orchestrator.adapters.truth.wiring import record_finding
            from cli_agent_orchestrator.core.findings import FindingCode

            if absent:
                observed = "no agent_status field in the pane response"
            elif detected_agent:
                observed = (
                    f"herdr agent_status={agent_status!r} for detected agent "
                    f"{detected_agent!r} (classification gap; usually transient)"
                )
            else:
                observed = (
                    f"herdr agent_status={agent_status!r} and NO agent detected "
                    f"(no provider process alive in this pane: crashed, never "
                    f"started, or already exited)"
                )
            record_finding(
                FindingCode.DIAG_HERDR_STATUS_UNKNOWN,
                terminal_id=terminal_id,
                # N5: the finding's dedupe identity and the warn-once set's key
                # are the SAME seat. The store already scopes a row by
                # (code, terminal_id, dedupe_key) and terminal_id carries the
                # session-unique id, so the window alone is the right dedupe_key
                # here — _seat_key names the pairing the log side needs.
                dedupe_key=_seat_key(session_name, window_name)[1],
                detail=(
                    f"{observed} for pane {pane_id} (session={session_name}, "
                    f"window={window_name}); no native status, falling back to "
                    f"pane scraping"
                ),
            )
        except Exception:  # noqa: BLE001 — an observation must never break the poll
            logger.debug("herdr status-unknown finding could not be recorded", exc_info=True)
        seat = _seat_key(session_name, window_name)
        with _STATUS_UNKNOWN_LOCK:
            first_for_seat = seat not in _STATUS_UNKNOWN_WARNED
            if first_for_seat:
                if len(_STATUS_UNKNOWN_WARNED) >= _STATUS_UNKNOWN_WARNED_MAX:
                    _STATUS_UNKNOWN_WARNED.clear()
                _STATUS_UNKNOWN_WARNED.add(seat)
        log = logger.warning if first_for_seat else logger.debug
        log(
            "herdr_status_unknown session=%s window=%s pane=%s agent_status=%s "
            "detected_agent=%s — no native status for this seat; falling back to "
            "pane scraping. No detected agent means no provider process is alive "
            "in the pane, NOT a launch-shape or manifest problem (F926 #778)",
            session_name,
            window_name,
            pane_id,
            "<absent>" if absent else agent_status,
            detected_agent or "<none>",
        )

    def get_native_status(self, session_name: str, window_name: str) -> Optional[TerminalStatus]:
        """Compatibility projection of :meth:`fetch_native_status`."""
        return self.fetch_native_status(session_name, window_name).status

    def _pane_get(self, pane_id: str) -> Dict[str, object] | object:
        """The pane mapping from ``herdr pane get``, or ``_PANE_GET_UNREADABLE``.

        N5: every reader of a pane's herdr state goes through here.
        ``fetch_native_status`` and ``probe_agent_detected`` were asking the same
        question with the same argv, the same envelope unwrap and the same
        exception set, written twice. Two copies of one protocol assumption
        drift the next time herdr renames a field: one gets fixed and the other
        quietly answers "absent" forever, which for the launch gate means
        tearing live seats down.

        Returns the WHOLE mapping rather than one field so both callers project
        what they need from a single reply — reading two fields must not mean
        two round trips, and must not let the two see different moments.
        """
        result = self._run_herdr(["pane", "get", pane_id], check=False)
        if result.returncode != 0:
            return _PANE_GET_UNREADABLE
        try:
            data = self._parse_herdr_json(result.stdout)
            pane_info = data.get("pane", data) if isinstance(data, dict) else data
            if not isinstance(pane_info, dict):
                return _PANE_GET_UNREADABLE
            return cast(Dict[str, object], pane_info)
        except (json.JSONDecodeError, AttributeError, TypeError):
            return _PANE_GET_UNREADABLE

    def probe_agent_detected(
        self, session_name: str, window_name: str, *, pane_id: Optional[str] = None
    ) -> Optional[bool]:
        """F935 (#787): has herdr named an agent for this pane?

        Reads the same ``pane get`` field :meth:`fetch_native_status` keys its
        diagnostic on. Measured on herdr 0.9.0: the name appears about a second
        after the process starts and the pane classifies by four, while a pane
        with nothing alive in it never gets a name at all.

        ``None`` when herdr could not be asked — an unresolved pane, a failed
        command, an unparseable reply. A transport fault must not read as "no
        agent", or a blip during startup would tear down a healthy seat.
        """
        if pane_id is None:
            try:
                pane_id = self._resolve_pane_id_from_window(session_name, window_name)
            except TerminalBackendError:
                return None

        pane_info = self._pane_get(pane_id)
        if pane_info is _PANE_GET_UNREADABLE:
            return None
        agent = cast(Dict[str, object], pane_info).get("agent", _PANE_GET_ABSENT)
        if agent is _PANE_GET_ABSENT:
            # herdr answered and named nothing. That is a real negative — and at
            # t=0 it is also the NORMAL state of a healthy pane, which is why the
            # caller must not act on a single sample (F935 r2 B1).
            return False
        return isinstance(agent, str) and bool(agent)

    def probe_provider_liveness(
        self,
        session_name: str,
        window_name: str,
        *,
        shell_baseline: Optional[str],
    ) -> "LivenessVerdict":
        """F880 (#733): herdr launch-health liveness via ``herdr pane process-info``.

        herdr owns the provider process — the agent child is NOT a descendant of
        CAO's own pane pid tree the way a tmux-spawned shell is, so the tmux path
        (``tmux list-panes`` → procfs ``_descendants``) has no answer for a herdr
        workspace and raised, failing every spawn ``provider_launch_failed``.
        herdr instead reports the pane's live foreground processes; a real
        provider child is present exactly when that list is non-empty AND names
        something other than a bare login shell.

        Verdict:
        - ``"dead"`` — the pane cannot be resolved, or its only foreground
          process is a bare shell equal to ``shell_baseline`` (an empty seat,
          the same "shell never exec-replaced" signal the tmux path uses).
        - ``"alive"`` — a foreground process is present that is not the baseline
          shell (the provider exec-replaced the shell or runs beneath it).
        - ``"unknown"`` — herdr could not answer (process-info returned nothing
          / errored), or there is no ``shell_baseline`` to disambiguate a lone
          shell from a real child; the caller degrades to the watchdog.

        This mirrors the tmux exec-replacement test rather than counting a
        descendant tree, because herdr's ``foreground_processes`` is a pane-local
        view, not a pid ancestry CAO can walk.
        """
        try:
            pane_id = self._resolve_pane_id_from_window(session_name, window_name)
        except TerminalBackendError:
            return "dead"

        result = self._run_herdr(["pane", "process-info", "--pane", pane_id], check=False)
        if result.returncode != 0:
            # herdr could not answer (transient socket/CLI error) — inconclusive,
            # not a confirmed death.
            return "unknown"
        try:
            data = self._parse_herdr_json(result.stdout)
            processes = self._foreground_processes(data)
        except (json.JSONDecodeError, AttributeError, TypeError):
            return "unknown"

        if not processes:
            # No foreground process at all: an empty seat / vanished pane.
            return "dead"

        names = [p.get("name") for p in processes if isinstance(p, dict)]
        names = [n for n in names if isinstance(n, str) and n]
        if not names:
            return "unknown"

        # Without a baseline shell to compare against, a foreground process
        # could be either the idle login shell or a real provider child — the
        # exec-replacement test cannot fire, so the seat is inconclusive.
        if not shell_baseline:
            return "unknown"

        # A process whose name is not the baseline shell is a live provider
        # child (exec-replaced or nested beneath the shell).
        if any(name != shell_baseline for name in names):
            return "alive"

        # Every foreground process equals the baseline shell → empty seat.
        return "dead"

    def get_pane_process_id(self, session_name: str, window_name: str) -> int:
        """F893 (#745): herdr's runtime-identity process root.

        herdr owns the provider child; there is no CAO-visible pane shell whose
        pid could be walked, and ``tmux list-panes`` (what every caller used
        before this port method existed) has no answer for a herdr workspace —
        it raised ``CalledProcessError`` and failed the F829 runtime-identity
        capture for grok workers live on grok-box-009. ``herdr pane
        process-info`` already reports the pane's live foreground processes with
        their pids, so the FIRST of them is the provider process itself.

        That is a valid root for both consumers: ``_descendants`` includes its
        own root, so an fd scan rooted at the provider still sees the provider's
        open rollout file, and ``pane_launch_epoch`` on the provider pid dates
        the provider launch more tightly than the shell's would.

        Raises:
            TerminalNotFoundError: the pane cannot be resolved.
            TerminalBackendError: herdr could not answer, or the pane reports no
                foreground process with a usable pid (an empty seat).
        """
        pane_id = self._resolve_pane_id_from_window(session_name, window_name)

        result = self._run_herdr(["pane", "process-info", "--pane", pane_id], check=False)
        if result.returncode != 0:
            raise TerminalBackendError(
                f"herdr pane process-info failed for {session_name}:{window_name}: "
                f"rc={result.returncode} {result.stderr.strip()}"
            )
        try:
            data = self._parse_herdr_json(result.stdout)
        except json.JSONDecodeError as e:
            raise TerminalBackendError(
                f"Failed to parse herdr pane process-info for {session_name}:{window_name}: {e}"
            ) from e
        for process in self._foreground_processes(data):
            pid = process.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                return pid
        raise TerminalBackendError(
            f"herdr pane {pane_id} ({session_name}:{window_name}) reports no "
            "foreground process pid"
        )

    def get_pane_id(self, terminal_id: str, session_name: str = "", window_name: str = "") -> str:
        """Resolve CAO terminal_id to herdr pane_id.

        Prefers the durable ``_pane_id_map`` (rebuilt from ``api snapshot``).
        Herdr 0.7.x public pane_ids are stable except across a full server
        restart, so a hit is returned directly and a miss triggers a single
        snapshot refresh before retrying the map. Only if the map still cannot
        resolve the terminal does resolution fall back to the legacy
        ``_pane_cache`` fast path and label-based window resolution
        (``_resolve_workspace_id`` -> ``_resolve_tab_id`` -> pane list). The
        legacy fallback is retained for reversibility and removed in a
        follow-up once the durable map is proven.

        Args:
            terminal_id: CAO UUID terminal identifier
            session_name: Optional session name for window-based fallback lookup
            window_name: Optional window name for window-based fallback lookup

        Returns:
            Current herdr compact pane_id

        Raises:
            TerminalNotFoundError: If pane cannot be resolved
        """
        # Durable map (rebuilt from api snapshot). Trust a hit only while the map
        # is fresh; herdr IDs are stable except across a server restart, which
        # this TTL bounds — a stale entry expires and the next lookup refreshes.
        # The map is keyed by the (session, window) labels CAO writes, so a
        # caller supplying neither cannot be answered from it. Gating on that
        # stops such a caller paying an `api snapshot` per call to rebuild a map
        # whose key it does not hold — it would miss either way.
        map_key = (session_name, window_name)
        if session_name and window_name:
            if (
                time.time() - self._pane_id_map_ts
            ) < _PANE_ID_MAP_TTL and map_key in self._pane_id_map:
                return self._pane_id_map[map_key]
            # Map is stale (or a miss). Rebuild, then trust it ONLY if the
            # rebuild succeeded — _refresh_pane_id_map leaves the timestamp
            # untouched on failure, so re-check freshness here. Without this
            # re-gate a failed refresh would return the very entry we just
            # judged expired, defeating the self-healing this TTL exists to
            # provide (fall through to the label-based fallback instead). It is
            # also what makes invalidate_pane's zeroed stamp hold when the
            # server is unreachable.
            self._refresh_pane_id_map()
            if (
                time.time() - self._pane_id_map_ts
            ) < _PANE_ID_MAP_TTL and map_key in self._pane_id_map:
                return self._pane_id_map[map_key]

        # Legacy fallback (removed in a follow-up once the map is proven):
        if terminal_id in self._pane_cache:
            from cli_agent_orchestrator.utils.tombstones import tombstone

            tombstone("TS-0004")
            pane_id, cached_at = self._pane_cache[terminal_id]
            if time.time() - cached_at < _PANE_CACHE_TTL:
                return pane_id
        if session_name and window_name:
            from cli_agent_orchestrator.utils.tombstones import tombstone

            tombstone("TS-0005")
            return self._resolve_pane_id_from_window(session_name, window_name)

        raise TerminalNotFoundError(terminal_id)

    def invalidate_pane(
        self, terminal_id: str, session_name: str = "", window_name: str = ""
    ) -> None:
        """Force the next :meth:`get_pane_id` to re-resolve against herdr.

        F930 (#782) made ``_pane_id_map`` authoritative for the first time — it
        had never once been hit before — and it sits IN FRONT of
        ``_pane_cache``. The herdr inbox reconcile invalidates a pane it has
        just proven dead by dropping the cache entry; with a live map in front
        of that cache the drop would no longer change the answer, and
        ``get_pane_id`` could hand back the very id the caller proved wrong. The
        reconcile would then "re-map" a terminal onto its own stale pane, count
        it repaired, and leave the routing table pointing at a pane id that is
        no longer that terminal's.

        So invalidation clears BOTH layers and the map's freshness stamp with
        them. Zeroing ``_pane_id_map_ts`` is what carries the guarantee when the
        caller cannot name the (session, window) key — and, because a failed
        refresh deliberately leaves map and stamp untouched, it is also what
        makes a refresh that cannot reach the server fall through to the live
        label walk instead of answering from stale memory.
        """
        self._pane_cache.pop(terminal_id, None)
        if session_name and window_name:
            self._pane_id_map.pop((session_name, window_name), None)
        self._pane_id_map_ts = 0.0

    def _refresh_pane_id_map(self) -> None:
        """Rebuild terminal_id -> pane_id from a live `api snapshot`.

        Public IDs are stable except across a full herdr server restart, so this
        is only needed on a miss (or at reconcile time), not per-call.

        On any failure — non-zero exit, a raising ``_run_herdr`` (subprocess
        timeout / missing binary surface as ``TerminalBackendError``; other
        ``OSError`` subtypes can surface directly), or an unparseable snapshot —
        the map and its timestamp are left unchanged. This keeps a failed
        refresh from marking a stale map as fresh and from propagating out of
        ``get_pane_id`` (which would skip the legacy fallback). ``_pane_id_map_ts``
        is stamped only after a successful rebuild.
        """
        try:
            result = self._run_herdr(["api", "snapshot"], check=False)
            if result.returncode != 0:
                return
            data = self._parse_herdr_json(result.stdout)
            snapshot = data.get("snapshot", data)
            if not isinstance(snapshot, dict):
                return
            # F930 (#782): key on the labels CAO writes, not on herdr's own
            # ``terminal_id``. A snapshot pane's ``terminal_id`` is HERDR's
            # terminal handle (``term_65b3308e082471``), never CAO's terminal
            # uuid (``919751d7``), so a map built from it could not be hit by
            # ``get_pane_id``'s CAO-keyed lookup even once: every call missed,
            # paid a full ``api snapshot`` subprocess, missed again and fell
            # through to the legacy label walk. The durable map was dead weight
            # that made every resolution SLOWER than having no map at all.
            #
            # The snapshot does carry CAO identity, one level up: CAO creates
            # each workspace labelled with its session name and each tab
            # labelled with its window name (``create_window`` passes
            # ``--label window_name``), and a pane names its ``tab_id``. Joining
            # panes → tabs → workspaces on those ids recovers exactly the
            # (session, window) pair callers ask with.
            #
            # The key is the PAIR, not the window alone: a snapshot spans every
            # workspace on the server, so two CAO sessions on one herdr server
            # would otherwise collide on a shared window name.
            workspace_labels = {
                w["workspace_id"]: w["label"]
                for w in snapshot.get("workspaces", [])
                if w.get("workspace_id") and w.get("label")
            }
            tabs = {
                t["tab_id"]: (workspace_labels.get(t.get("workspace_id", "")), t["label"])
                for t in snapshot.get("tabs", [])
                if t.get("tab_id") and t.get("label")
            }
            rebuilt: Dict[tuple[str, str], str] = {}
            for pane in snapshot.get("panes", []):
                if not pane.get("pane_id"):
                    continue
                session_label, window_label = tabs.get(pane.get("tab_id", ""), (None, None))
                if session_label and window_label:
                    # FIRST pane wins, matching _resolve_pane_id_from_window
                    # (which returns the tab's first pane). CAO makes one pane
                    # per tab today so the two agree trivially — but a user
                    # splitting a pane inside a CAO workspace must not make the
                    # two resolution paths disagree by snapshot order.
                    rebuilt.setdefault((session_label, window_label), pane["pane_id"])
            self._pane_id_map = rebuilt
            self._pane_id_map_ts = time.time()
        except (
            TerminalBackendError,
            subprocess.SubprocessError,
            OSError,
            json.JSONDecodeError,
            KeyError,
            AttributeError,
            TypeError,
        ):
            return

    # --- Pipe-pane (no-op for herdr) ---

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        """No-op: herdr uses socket events for inbox delivery."""
        logger.debug(f"pipe_pane is a no-op for herdr backend (session={session_name})")

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        """No-op: herdr uses socket events for inbox delivery."""
        logger.debug(f"stop_pipe_pane is a no-op for herdr backend (session={session_name})")

    # --- Internal helpers ---

    def _session_socket_path(self) -> str:
        """Return the herdr socket path for the configured session.

        Delegates to the single herdr transport leaf
        ``adapters.herdr.client.default_socket_path`` (WP-HERDR H1, blueprint §4):
        the socket-path layout now has ONE definition, in the client, and this
        legacy shim imports it (legacy importing new code is permitted; the
        reverse is not). Byte-identical to the former inline resolution.
        """
        from cli_agent_orchestrator.adapters.herdr.client import default_socket_path

        return default_socket_path(self._herdr_session)

    @staticmethod
    def _socket_is_live(socket_path: str) -> bool:
        """Return True only if a herdr server is actually listening on the socket.

        F882 (#735): ``os.path.exists`` is not liveness. A herdr server that was
        SIGKILLed leaves its unix-socket inode on disk, so an existence check
        reports a dead session as running — that is A3 in the live report
        (``_ensure_session_running`` no-ops on a stale sock; ``GET /health``
        stays ``ok``). A real ``connect()`` to the unix socket distinguishes a
        listening server (connect succeeds) from a leftover inode with no
        listener (``ConnectionRefusedError``) and from a path that is gone
        (``FileNotFoundError``). Cheap, synchronous, and never raises: any
        failure is reported as "not live".
        """
        import socket as _socket

        if not os.path.exists(socket_path):
            return False
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            sock.settimeout(1.0)
            sock.connect(socket_path)
            return True
        except OSError:
            # ConnectionRefusedError (stale inode, no listener), timeout, or any
            # other socket error → not live.
            return False
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def backend_health(self) -> str:
        """F882 (#735): report herdr health from real socket liveness.

        Returns ``"ok"`` only when a herdr server is listening on the configured
        session socket, ``"socket_closed"`` when the socket path is present but
        no server answers (a SIGKILLed/exited session leaving a stale inode),
        and ``"unavailable"`` when the socket path does not exist at all. This
        replaces the health endpoint's ``shutil.which("herdr")`` probe, which
        only proved the binary is installed and reported ``ok`` across a dead
        socket.
        """
        socket_path = self._session_socket_path()
        if not os.path.exists(socket_path):
            return "unavailable"
        return "ok" if self._socket_is_live(socket_path) else "socket_closed"

    def _session_unit_name(self) -> Optional[str]:
        """Name of the systemd user unit that owns this session's herdr server.

        ``CAO_HERDR_UNIT`` overrides (empty string disables). The default only
        applies to the canonical ``cao`` session; other sessions have no unit.
        """
        env = os.environ.get("CAO_HERDR_UNIT")
        if env is not None:
            return env or None
        return "cao-herdr.service" if self._herdr_session == "cao" else None

    def _start_session_unit(self) -> bool:
        """Start the herdr server through its systemd user unit, if one exists.

        Keeps the herdr server OUT of cao-server's cgroup so a cao-server restart
        never signals it (the pre-unit behaviour: 45s stop timeout + SIGABRT +
        every live worker pane lost, 2026-09-11). Returns True when the unit was
        started (or is already active), False when systemd or the unit is
        unavailable so the caller can fall back to a plain spawn.
        """
        unit = self._session_unit_name()
        if not unit or shutil.which("systemctl") is None:
            return False
        try:
            probe = subprocess.run(
                ["systemctl", "--user", "cat", unit],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if probe.returncode != 0:
                return False
            res = subprocess.run(
                ["systemctl", "--user", "start", unit],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("herdr_unit_start_failed unit=%s error=%s", unit, exc)
            return False
        if res.returncode != 0:
            logger.warning(
                "herdr_unit_start_failed unit=%s rc=%s stderr=%s",
                unit,
                res.returncode,
                (res.stderr or "").strip(),
            )
            return False
        logger.info("Started herdr session '%s' via %s", self._herdr_session, unit)
        return True

    def _ensure_session_running(self) -> None:
        """Start the herdr session server if it is not actually listening.

        F882 (#735): checks socket LIVENESS, not mere existence. A herdr server
        that died by signal leaves its unix-socket inode behind, so the former
        ``os.path.exists`` short-circuit treated a dead session as running and
        every subsequent operation failed with ``server_not_running`` until the
        inode was unlinked by hand (A3 in the live report). Now a present but
        unresponsive socket is unlinked and the server restarted; a live socket
        still short-circuits exactly as before.

        Logs a warning if the socket never becomes live but does not raise —
        the first actual herdr operation will produce a clear error.
        """
        socket_path = self._session_socket_path()
        if self._socket_is_live(socket_path):
            return

        # A present-but-dead socket (stale inode from a SIGKILLed server) must be
        # removed before starting a new server, or herdr refuses to bind and the
        # session stays wedged. Best-effort: a race that removes it first, or a
        # permission error, is not fatal — the start attempt below still runs.
        if os.path.exists(socket_path):
            logger.warning(
                f"Herdr session '{self._herdr_session}' socket {socket_path} is present "
                f"but not accepting connections (stale) — unlinking before restart."
            )
            try:
                os.unlink(socket_path)
            except OSError as exc:
                logger.warning(
                    "herdr_stale_socket_unlink_failed session=%s path=%s error=%s",
                    self._herdr_session,
                    socket_path,
                    exc,
                )

        logger.info(
            f"Herdr session '{self._herdr_session}' not running "
            f"(socket {socket_path} not live) — starting server."
        )
        if not self._start_session_unit():
            # Fallback (no systemd, or no cao-herdr.service installed): spawn the
            # server as a detached child. NOTE: under systemd this lands the herdr
            # server inside cao-server's cgroup, so a cao-server restart kills it
            # and every live pane with it — install the unit to avoid that.
            subprocess.Popen(
                ["herdr", "--session", self._herdr_session, "server"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        # Give herdr a moment to create the socket file before polling.
        time.sleep(0.5)

        # Poll up to 15 seconds for the socket to become live.
        deadline = time.time() + 15.0
        max_iterations = max(1, int(15.0 / 0.1 * 3))
        iterations = 0
        while time.time() < deadline and iterations < max_iterations:
            iterations += 1
            if self._socket_is_live(socket_path):
                logger.info(f"Herdr session '{self._herdr_session}' is ready.")
                return
            time.sleep(0.1)

        if iterations >= max_iterations:
            logger.warning(
                "_ensure_herdr_running: iteration cap reached (%d), exiting", max_iterations
            )
        logger.warning(
            f"Herdr session '{self._herdr_session}' socket did not become live within 15s "
            f"at {socket_path}. The first herdr operation will fail with a clear error."
        )

    def _parse_new_pane_id(self, stdout: str) -> Optional[str]:
        """Extract the root pane_id from a workspace/tab create response.

        Both 'herdr workspace create' and 'herdr tab create' return a
        result.root_pane.pane_id field with the newly created pane's ID.
        """
        try:
            data = self._parse_herdr_json(stdout)
            return str(data["root_pane"]["pane_id"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def _build_env_args(
        self,
        terminal_id: str,
        session_name: str,
        extra_env: Optional[Dict[str, str]] = None,
        terminal_token: Optional[str] = None,
        allowed_blocked_values: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """Build ``--env KEY=VALUE`` argument pairs for a create command.

        Operator-forwarded vars are merged first, filtered with the same policy
        TmuxClient applies to its ``-e`` argv (blocked prefixes, per-value byte
        cap). The two CAO identity vars are assigned LAST so an operator
        ``--env CAO_TERMINAL_ID=...`` cannot override the real terminal identity
        (mirrors TmuxClient, which forces these to win). Native ``--env``
        replaces the former shell ``export`` injection, removing the
        command-line injection surface.

        ``allowed_blocked_values`` is an explicit per-key escape hatch for vars
        that match a blocked prefix but carry a known-safe value (e.g.
        CODEX_HOME injected by persona-context). Only the exact value listed is
        permitted; any other value is dropped with the standard warning. This
        mirrors the identical escape in TmuxClient._merge_extra_env.

        Note: on herdr, env VALUES pass through the herdr arg sanitizer, which
        rejects shell metacharacters and control chars. A value containing e.g.
        ``$ ; | & ! * ? < >`` will fail terminal creation on herdr (fail-closed),
        whereas the tmux backend accepts such values. This is an intentional,
        safety-conservative divergence; operator env values on herdr must be
        sanitizer-safe.
        """
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        _allowed = allowed_blocked_values or {}
        env: Dict[str, str] = {}
        for key, value in (extra_env or {}).items():
            if TmuxClient._is_blocked_env_key(key):
                # Plane-pin escape: value matches provider_plane_environment().
                if os.environ.get("CAO_INSTANCE_ID", "").strip() and key in {
                    "CODEX_HOME",
                    "CLAUDE_CONFIG_DIR",
                }:
                    from cli_agent_orchestrator.utils.provider_plane import (
                        provider_plane_environment,
                    )

                    if provider_plane_environment().get(key) == value:
                        env[key] = value
                        continue
                # Caller-threaded escape: value matches an explicitly allowed value.
                if key in _allowed and _allowed[key] == value:
                    env[key] = value
                    continue
                logger.warning("Dropping forwarded env var with blocked prefix: %s", key)
                continue
            if len(value.encode("utf-8")) >= TmuxClient._MAX_ENV_VALUE_BYTES:
                logger.warning("Dropping forwarded env var %s -- exceeds byte cap", key)
                continue
            env[key] = value

        # CAO identity vars are assigned last so operator-forwarded --env cannot
        # override them (mirrors TmuxClient, which forces these to win).
        env["CAO_TERMINAL_ID"] = terminal_id
        env["CAO_SESSION_NAME"] = session_name
        if terminal_token:
            env["CAO_TERMINAL_TOKEN"] = terminal_token

        args: List[str] = []
        for key, value in env.items():
            args.extend(["--env", f"{key}={value}"])
        return args

    def _resolve_tab_id(self, session_name: str, workspace_id: str, window_name: str) -> str:
        """Resolve window_name to its herdr tab_id in the given workspace.

        Args:
            session_name: CAO session name (used only in error messages)
            workspace_id: Herdr workspace ID to search within
            window_name: Tab label to match

        Returns:
            The tab_id of the matching tab

        Raises:
            TerminalBackendError: If no tab with label window_name exists in workspace_id
        """
        result = self._run_herdr(["tab", "list"])
        try:
            data = self._parse_herdr_json(result.stdout)
            tabs = data.get("tabs", []) if isinstance(data, dict) else data
        except json.JSONDecodeError as e:
            raise TerminalBackendError(f"Failed to parse herdr tab list: {e}") from e

        for tab in tabs:
            if tab.get("workspace_id") == workspace_id and tab.get("label") == window_name:
                return str(tab["tab_id"])

        raise TerminalBackendError(
            f"No tab labeled '{window_name}' found in workspace '{session_name}'"
        )

    def _resolve_pane_id_from_window(self, session_name: str, window_name: str) -> str:
        """Resolve a pane_id given session_name and window_name.

        Performs a fresh herdr workspace + tab + pane lookup on every call. A
        pane_id can go DEAD while its tab label lives — the pane was closed and
        re-created, or moved to another workspace (which mints a new
        workspace-qualified id), or the server restarted — so a cached pane_id
        would cause pane_not_found errors for live terminals.

        It is NOT that herdr renumbers siblings. Measured on herdr 0.9.0
        (protocol 22, grok-box-010), four tabs in one workspace, closing the
        middle one:

            BEFORE  w1:p1 tab-1   w1:p2 tab-a   w1:p3 tab-b   w1:p4 tab-c
            AFTER   w1:p1 tab-1                 w1:p3 tab-b   w1:p4 tab-c

        Surviving panes keep their ids and the closed id is retired, not reused
        (herdr's skill file: "Closed tab and pane IDs are not reused"). The
        conclusion — resolve live, never cache a pane_id here — is unchanged;
        only the reason it was written down was wrong. workspace_id
        resolution is cached with a short TTL inside _resolve_workspace_id as a
        latency optimization; the chain is otherwise resolved live.

        Resolution chain: workspace_id (by label) → tab_id (by label within the
        workspace) → the pane whose tab_id matches. There is no fallback: a tab
        must exist for the window and a pane must exist for the tab.

        Raises:
            TerminalNotFoundError: If the workspace, tab, or pane cannot be
                resolved for session_name:window_name.
        """
        try:
            workspace_id = self._resolve_workspace_id(session_name)
            tab_id = self._resolve_tab_id(session_name, workspace_id, window_name)

            result = self._run_herdr(["pane", "list"])
            try:
                data = self._parse_herdr_json(result.stdout)
                panes = data.get("panes", []) if isinstance(data, dict) else data
            except json.JSONDecodeError as e:
                raise TerminalBackendError(f"Failed to parse herdr pane list: {e}") from e
        except TerminalBackendError as e:
            raise TerminalNotFoundError(f"{session_name}:{window_name}") from e

        for pane in panes:
            if pane.get("tab_id") == tab_id:
                return str(pane["pane_id"])

        raise TerminalNotFoundError(f"{session_name}:{window_name}")
