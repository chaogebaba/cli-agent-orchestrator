"""Terminal backend abstract base class.

Defines the contract that all terminal backends (tmux, herdr, etc.) must satisfy.
Core services depend only on this ABC, never on a concrete backend directly.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

#: F880 (#733): the verdict a backend returns for a launch-health liveness
#: probe. Deliberately the same three-valued vocabulary ``_provider_child_alive``
#: already speaks (``True``/``False``/``None``) so the backend answers the exact
#: question the caller asks and the caller keeps its inconclusive-is-non-fatal
#: rule: ``"alive"`` == a live provider child, ``"dead"`` == a confirmed-empty
#: seat (raises ProviderLaunchFailed at the deadline), ``"unknown"`` == the
#: backend could not tell (procfs missing, no baseline to compare) and the
#: terminal is NOT failed on that basis.
LivenessVerdict = Literal["alive", "dead", "unknown"]


@dataclass(frozen=True)
class ScopeProbe:
    """F218-a D2: Result of a positive re-probe to classify loss scope."""

    scope: Literal["window_gone", "session_gone", "unknown"]
    session_present: bool | None  # None = could not ask (_has_session_via_cli semantics)
    sibling_windows: tuple[str, ...] | None
    samples: int
    evidence: tuple[str, ...]  # verbatim rc/stderr, oldest→newest


from cli_agent_orchestrator.models.terminal import TerminalStatus


class TerminalBackendError(Exception):
    """Base exception for terminal backend operations."""

    pass


class TerminalNotFoundError(TerminalBackendError):
    """Raised when a terminal/pane cannot be found or resolved."""

    def __init__(self, terminal_id: str, message: Optional[str] = None):
        self.terminal_id = terminal_id
        super().__init__(message or f"Terminal not found: {terminal_id}")


@dataclass(frozen=True)
class PaneIdentityReadResult:
    identity: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.identity is None) == (self.reason is None):
            raise ValueError("exactly one pane identity result field must be set")
        if self.reason is not None and self.reason not in {
            "missing_env",
            "read_error",
            "pane_cardinality",
            "incarnation_changed",
        }:
            raise ValueError(f"invalid pane identity failure reason: {self.reason}")


@dataclass(frozen=True)
class NativeIdentityResult:
    """Backend-native agent identity for panes without env readback."""

    agent: str | None
    foreground_process: str | None
    verdict: Literal["match", "mismatch", "unavailable"]

    def __post_init__(self) -> None:
        if self.verdict == "match" and self.agent is None:
            raise ValueError("native identity match requires an agent marker")
        if self.verdict == "mismatch" and self.agent is None:
            raise ValueError("native identity mismatch requires an agent marker")
        if self.verdict == "unavailable" and self.agent is not None:
            raise ValueError("unavailable native identity cannot carry an agent marker")


class TerminalBackend(ABC):
    """Abstract base class defining the terminal backend contract.

    All terminal operations CAO requires are declared here. Concrete backends
    (TmuxBackend, HerdrBackend) implement these methods using their respective
    multiplexer APIs.
    """

    supports_identity_readback = False

    def read_native_identity(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        expected_provider: str,
    ) -> NativeIdentityResult:
        """Return provider identity from a backend-native authority when available."""
        return NativeIdentityResult(None, None, "unavailable")

    # --- Session lifecycle ---

    @abstractmethod
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
        """Create a new terminal session with an initial window.

        Args:
            session_name: Name for the session (e.g., "cao-my-project")
            window_name: Name for the initial window/tab
            terminal_id: Unique terminal identifier to inject into the environment
            working_directory: Optional starting directory
            terminal_token: Optional per-terminal auth token to inject into the environment

        Returns:
            The actual window name assigned by the backend

        Raises:
            TerminalBackendError: If session creation fails
            ValueError: If working_directory is invalid
        """
        ...

    @abstractmethod
    def session_exists(self, session_name: str) -> bool:
        """Check if a session exists.

        Args:
            session_name: Session name to check

        Returns:
            True if the session exists
        """
        ...

    def session_exists_strict(self, session_name: str) -> bool:
        """Check if a session exists, RAISING when the lookup cannot be answered.

        Semantics differ from ``session_exists`` only in the error case:
        ``session_exists`` collapses a lookup error into False ("assume
        absent"), whereas this must distinguish a confirmed absence (return
        False) from an inability to tell (raise). Teardown confirmation MUST
        use this so a transient backend error is never misread as "session
        gone" (#498).

        Implementations must query in a way that keeps the two apart. That is a
        real constraint, not a formality: a client library that swallows its own
        transport errors and reports an empty result set makes a lookup failure
        LOOK like an absence, and a strict check layered over it fails OPEN no
        matter what it does with its own exceptions. ``TmuxBackend`` therefore
        issues its own ``list-sessions`` and classifies the exit status
        (``clients/tmux.py``) rather than using libtmux's session collection.

        The default implementation delegates to ``session_exists`` for backends
        that cannot yet make the distinction; those backends therefore retain the
        old lenient, fail-OPEN behavior, and the teardown guarantee is only as
        strong as this method. HerdrBackend is in that position — tracked as a
        follow-up.
        """
        return self.session_exists(session_name)

    @abstractmethod
    def list_sessions(self) -> List[Dict[str, str]]:
        """List all sessions managed by this backend.

        Returns:
            List of dicts with keys: id, name, status
        """
        ...

    @abstractmethod
    def kill_session(self, session_name: str) -> bool:
        """Kill/destroy a session.

        Implementations MUST NOT return True on a merely-dispatched kill: session
        teardown treats True as proof the session is gone and only then drops the
        matching registry rows, so an optimistic True is what lets tmux and the
        registry diverge (#498). Confirm the session is actually gone — poll if
        the kill is asynchronous — before returning True.

        Args:
            session_name: Session to kill

        Returns:
            True once the session is confirmed gone; False if it was not found
            OR the kill could not be confirmed within the backend's bound. A
            caller that must tell those two apart re-checks existence itself via
            ``session_exists_strict``.
        """
        ...

    # --- Window/tab lifecycle ---

    def window_liveness(self, session_name: str, window_name: str) -> str:
        """Return live, gone, or error without collapsing backend failures."""
        return "error"

    def enumerate_windows(
        self, session_name: str
    ) -> tuple[Literal["ok", "error"], List[Dict[str, object]] | None]:
        """Enumerate windows via subprocess. Classifies its own failure.

        Returns ("ok", [...]) on success, ("ok", []) when the session is
        genuinely absent, or ("error", None) when the read itself failed.
        """
        return ("error", None)

    def session_scope_probe(
        self,
        session_name: str,
        *,
        window_name: str,
        samples: int = 2,
        timeout_s: float = 5.0,
    ) -> ScopeProbe:
        """F218-a D2: Classify scope of a confirmed-gone terminal.

        Determines whether the window alone is gone (session still alive with
        siblings) or the entire session is gone. Returns ``unknown`` when the
        answer cannot be determined reliably.

        The default implementation always returns ``unknown``.
        """
        return ScopeProbe(
            scope="unknown",
            session_present=None,
            sibling_windows=None,
            samples=0,
            evidence=("default_backend_no_probe",),
        )

    def get_session_windows(self, session_name: str) -> List[Dict[str, object]]:
        """Return the windows visible in a session, or an empty inventory."""
        return []

    @abstractmethod
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
        """Create a new window/tab in an existing session.

        Args:
            session_name: Session to add the window to
            window_name: Name for the new window
            terminal_id: Unique terminal identifier to inject into the environment
            working_directory: Optional starting directory
            window_shell: Optional shell command to run instead of default shell
            terminal_token: Optional per-terminal auth token to inject into the environment

        Returns:
            The actual window name assigned by the backend

        Raises:
            TerminalBackendError: If window creation fails
            ValueError: If session not found or working_directory is invalid
        """
        ...

    @abstractmethod
    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Kill a specific window within a session.

        Args:
            session_name: Session containing the window
            window_name: Window to kill

        Returns:
            True if window was killed, False if not found
        """
        ...

    # --- Input ---

    @abstractmethod
    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
    ) -> None:
        """Send text input to a window.

        Args:
            session_name: Target session
            window_name: Target window
            keys: Text to send
            enter_count: Number of Enter keys to send after the text
            force_bracketed_paste: If True, request bracketed-paste delivery.
                The herdr backend wraps content in \\x1b[200~...\\x1b[201~
                itself (it writes raw bytes to the pty, no sanitization). The
                tmux backend hand-crafts the same wrap on tmux < 3.7 but must
                delegate to ``paste-buffer -p`` on >= 3.7, where pasted
                buffers are vis(3)-sanitized and raw ESC bytes would arrive
                as literal "^[[200~" (issue #413); -p emits markers only when
                the pane enabled DECSET 2004.
            submit_delay: Seconds to wait after pasting before sending Enter, so
                a TUI (e.g. Claude Code's Ink renderer) finishes processing the
                paste before submission. Backends without a paste step may ignore.
        """
        ...

    @abstractmethod
    def send_special_key(self, session_name: str, window_name: str, key: str) -> None:
        """Send a special key (e.g., C-c, C-d, Enter) to a window.

        Unlike send_keys(), this sends the key as a control/special key name
        and does not append a carriage return.

        Args:
            session_name: Target session
            window_name: Target window
            key: Key name (e.g., "C-d", "C-c", "Escape", "Enter", "")
        """
        ...

    # --- Output ---

    @abstractmethod
    def get_history(
        self,
        session_name: str,
        window_name: str,
        tail_lines: Optional[int] = None,
        strip_escapes: bool = False,
        full_history: bool = False,
        visible_only: bool = False,
    ) -> str:
        """Get terminal output/history from a window.

        Args:
            session_name: Target session
            window_name: Target window
            tail_lines: Number of lines from the end (None = backend default).
                On tmux this INCLUDES the visible pane plus N lines of scrollback
                above it — it is not a viewport bounded to N rows.
            strip_escapes: If True, strip ANSI escape sequences
            full_history: If True, capture entire scrollback
            visible_only: If True, capture only the currently rendered viewport,
                nothing from scrollback (overrides tail_lines/full_history). A
                backend without a viewport concept may approximate with its
                closest bounded recent read.

        Returns:
            Terminal output as a string
        """
        ...

    def capture_viewport(self, session_name: str, window_name: str) -> str:
        """Capture the current escape-normalized viewport without scrollback.

        Backends that cannot provide this exact freshness primitive fail closed
        at admission rather than approximating it with terminal history.
        """
        raise NotImplementedError("viewport capture is not supported by this backend")

    def read_pane_identity(self, session_name: str, window_name: str) -> PaneIdentityReadResult:
        return PaneIdentityReadResult(reason="read_error")

    @abstractmethod
    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        """Get the current working directory of a pane.

        Args:
            session_name: Target session
            window_name: Target window

        Returns:
            Working directory path, or None if unavailable
        """
        ...

    @abstractmethod
    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        """Get the current foreground command running in a pane.

        Args:
            session_name: Target session
            window_name: Target window

        Returns:
            Command name, or None if unavailable
        """
        ...

    def get_pane_size(self, session_name: str, window_name: str) -> Optional[tuple]:
        """Get the (columns, rows) of a pane's real viewport.

        Non-abstract with a None default: only screen-rendering consumers
        (StatusMonitor's pyte path) need it, and only the tmux backend can
        answer. None means "unknown — use configured fallback dimensions".

        Returns:
            (columns, rows) tuple, or None if unavailable
        """
        return None

    # --- Attach ---

    @abstractmethod
    def attach_session(self, session_name: str) -> None:
        """Attach to a session (for interactive use).

        Args:
            session_name: Session to attach to
        """
        ...

    @abstractmethod
    def prepare_web_attach(self, session_name: str, window_name: str) -> List[str]:
        """Prepare a browser PTY attachment and return its subprocess argv.

        Backends may perform routing work before returning, such as focusing a
        Herdr workspace/tab. The caller owns the PTY and subprocess lifecycle.

        Args:
            session_name: Target session
            window_name: Target window

        Returns:
            Subprocess argv for the interactive backend client

        Raises:
            TerminalBackendError: If the backend cannot prepare the attachment
        """
        ...

    # --- Pipe-pane (logging) ---

    @abstractmethod
    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        """Start piping pane output to a file.

        For backends that don't support pipe-pane (e.g., herdr), this is a no-op
        since inbox delivery uses a different mechanism.

        Args:
            session_name: Target session
            window_name: Target window
            file_path: Absolute path to the log file
        """
        ...

    @abstractmethod
    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        """Stop piping pane output.

        For backends that don't support pipe-pane, this is a no-op.

        Args:
            session_name: Target session
            window_name: Target window
        """
        ...

    # --- Capability queries ---

    def supports_event_inbox(self) -> bool:
        """Whether this backend uses event-based inbox delivery (e.g., socket events).

        When True, terminals should be registered with an event-based inbox service
        instead of using pipe-pane file watching.

        Default is False (pipe-pane based delivery).
        """
        return False

    def get_pane_id(self, terminal_id: str, session_name: str = "", window_name: str = "") -> str:
        """Resolve terminal_id to backend-specific pane identifier.

        Only meaningful for backends that use event-based inbox delivery.
        Default raises NotImplementedError.

        Args:
            terminal_id: CAO terminal identifier
            session_name: Optional session name for window-based fallback lookup
            window_name: Optional window name for window-based fallback lookup

        Returns:
            Backend-specific pane identifier

        Raises:
            NotImplementedError: If backend does not support pane ID resolution
        """
        raise NotImplementedError(f"{type(self).__name__} does not support get_pane_id()")

    def probe_agent_detected(self, session_name: str, window_name: str) -> Optional[bool]:
        """Has this backend RECOGNISED an agent in the pane? (F935 #787)

        Distinct from :meth:`probe_provider_liveness`, which asks whether a
        process is alive. A wrapper or runtime that starts and stays up is alive
        without an agent ever appearing inside it, and a seat the backend never
        recognises carries no native status for delivery to wait on.

        Returns:
            ``True``  — an agent is named for the pane;
            ``False`` — the backend answered and named none;
            ``None``  — this backend has NO OPINION, and callers must skip the
            gate entirely rather than read it as a negative. That is the default
            here, so a backend without agent awareness (tmux) is unaffected by
            anything built on this.
        """
        return None

    def invalidate_pane(
        self, terminal_id: str, session_name: str = "", window_name: str = ""
    ) -> None:
        """Drop every cached answer this backend holds for one terminal's pane.

        A caller that has just PROVEN a pane id wrong (the herdr inbox
        reconcile, which finds a pane id dead while its tab label is still
        live) must be able to force the next :meth:`get_pane_id` to re-resolve
        against the live server. Before F930 that caller reached into
        ``backend._pane_cache`` directly, which worked only because the other
        cache in front of it never hit; naming the operation makes the
        invalidation a contract instead of a coincidence.

        Default is a no-op: a backend with no pane caches has nothing to drop.
        """
        return None

    def get_native_status(self, session_name: str, window_name: str) -> Optional[TerminalStatus]:
        """Query native agent status if the backend has agent awareness.

        Returns None if unsupported — caller falls back to pane content parsing.
        """
        return None

    # --- Launch-health liveness (F880 #733) ---

    def probe_provider_liveness(
        self,
        session_name: str,
        window_name: str,
        *,
        shell_baseline: Optional[str],
    ) -> LivenessVerdict:
        """Classify whether a provider child is alive in this window's seat.

        This is the backend-specific half of ``_provider_child_alive`` (F124):
        the caller owns the backend-agnostic short-circuits (process-less
        providers, a provider that already confirmed a fixture-child death),
        and delegates the "is a real provider process running behind this seat"
        question HERE so a backend answers it with its OWN authority instead of
        the caller reaching into tmux verbs. F880 (#733): the herdr launch-health
        failure was exactly this — the shared caller resolved a pane pid via
        ``tmux list-panes`` and walked procfs, which a herdr workspace has no
        answer for, so every herdr spawn died ``provider_launch_failed``.

        Returns a three-valued :data:`LivenessVerdict`:
        - ``"alive"`` — a provider child is confirmed running.
        - ``"dead"`` — the seat is confirmed empty (bare shell / vanished).
        - ``"unknown"`` — the backend cannot tell (missing procfs, no baseline
          to compare a foreground command against); the caller treats this as
          non-fatal and degrades to the watchdog rather than failing the
          terminal.

        ``shell_baseline`` is the provider's captured idle-shell command name
        (e.g. ``"bash"``); a backend that classifies by comparing the live
        foreground command against it needs the baseline and returns ``"unknown"``
        when it is absent.

        The default raises so a backend that has not implemented a liveness
        primitive is a loud programming error, not a silent always-alive.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement probe_provider_liveness()"
        )

    def supports_status_decorations(self) -> bool:
        """Whether this backend has tmux-style per-session user options.

        F893 (#745) bug-family sweep: ``boundary_pull_service`` writes the
        ``@cao_pending`` user option for the tmux status line on every recount.
        That is a tmux UI affordance with no herdr equivalent, so it is
        tmux-only BY DESIGN — but under herdr it ran anyway and logged a
        ``set-option`` failure warning on every message round. Callers of such
        decorations gate on this instead of shelling out and warning.
        """
        return False

    # --- Runtime-identity process root (F893 #745) ---

    def get_pane_process_id(self, session_name: str, window_name: str) -> int:
        """Return the pid that roots the provider process tree behind this seat.

        This is the process an identity capture may walk descendants of
        (``capture_codex_uuid`` scans ``/proc/<pid>/fd`` for the rollout file)
        and whose ``/proc`` start time dates the provider launch
        (``pane_launch_epoch``). Both consumers only need *a* local pid at or
        above the provider in the tree, which is why one port method serves
        backends whose seat model differs:

        - **tmux** returns the window's FIRST pane pid (lowest ``pane_index``,
          the F545/#401 rule) — the login shell the provider was exec'd into,
          so the provider is a descendant.
        - **herdr** owns the child itself and has no CAO-visible pane shell, so
          it returns the pane's first live foreground process pid, i.e. the
          provider process directly. ``_descendants`` includes its root, so an
          fd scan rooted there still finds the provider's own open files.

        F893 (#745): ``_prepare_provider_runtime_identity`` and six sibling
        sites resolved this pid by calling ``fork_context_service.pane_pid``,
        which shells out to ``tmux list-panes`` unconditionally. Under the herdr
        backend that raises ``CalledProcessError`` and fails the F829
        runtime-identity capture for every ``supports_reauth_rebind`` provider
        (observed live on grok-box-009 for grok workers). Same class as F880
        (#733); the fix is the same — ask the backend.

        Raises:
            TerminalBackendError / TerminalNotFoundError: the seat cannot be
                resolved or the backend cannot name a process for it. Callers
                keep whatever failure handling they had for the old tmux
                ``CalledProcessError``.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement get_pane_process_id()")

    # --- Backend health (F882 #735) ---

    def backend_health(self) -> str:
        """Return this backend's live health for ``GET /health``.

        ``"ok"`` when the backend's control plane is reachable; a backend-specific
        NON-``"ok"`` string (e.g. ``"unavailable"``, ``"socket_closed"``) when it
        is not. F882 (#735): the health endpoint reported the herdr component
        ``ok`` from ``shutil.which("herdr")`` alone, so a dead herdr socket still
        read healthy. A backend that can cheaply probe its control plane
        overrides this; the default returns ``"ok"`` because a backend with no
        separate control plane (tmux drives panes through per-call subprocess
        invocations) has nothing to lose independently of the server process.
        """
        return "ok"
