"""F899 (#751): child-process liveness probe for the pane-hold expiry.

Why this exists
---------------
``status_monitor.fuse_status`` rule 3b admits the provider-published status
(usually IDLE) the moment ``liveness.pane_delta_max_hold_s`` expires with the
pane still churning. That bound is correct for a *blinking* pane, but it is
wrong for the observed #751 case: a worker running a long, silent box command
(``grokfleet lease run ... pytest``) renders a live elapsed-time counter, so the
pane never stops changing, the hold clock never resets, and at 300 s the seat is
published ``idle`` while real work is in flight — twice observed 2026-09-10 with
``since_last_input`` 877 s / 1136 s.

The evidence the pane cannot supply is on the machine: the worker's process
tree. This module reads it and answers ONE question — *is a tool subprocess
alive under this pane?* — so the expiry can fall to idle only when nothing is
running.

The discriminator (provider-agnostic, no text matching on provider names)
------------------------------------------------------------------------
Measured live (2026-09-10) on this fleet, an idle ``pi_cli`` worker's tree is::

    zsh (pane pid)            depth 0
    └── pi                    depth 1     the provider itself
        └── cao-mcp-server    depth 2     PERSISTENT stdio helper

and a working one is::

    zsh                       depth 0
    └── pi                    depth 1
        └── bash              depth 2     the provider's Bash tool
            └── sleep/ssh/…   depth 3+

So "any descendant deeper than the provider" is NOT usable: it would count the
persistent MCP helper and pin every such worker PROCESSING forever, recreating
the monotonic-state failure #361 forbids. What separates the two trees is that
CLI agents run tool work *through a shell* (``bash -c`` / ``sh -c``), while
long-lived stdio helpers are exec'd directly. Hence:

  ``base_depth`` = 1 when the pane pid's own comm is a shell (the login shell
  tmux started), else 0 — i.e. the depth at which the provider process sits.

  A descendant counts as LIVE WORK when its depth exceeds ``base_depth`` and it
  is either a shell itself or a descendant of one such shell.

Only generic shell names are consulted (``liveness.child_proc_shell_comms``),
never a provider name, so the rule holds for claude/codex/pi/kiro/grok alike.
It also degrades correctly when a provider is launched as the pane command
itself (``base_depth`` 0, tool shells at depth 1).

Contract
--------
``probe()`` NEVER raises: every failure (no metadata, unresolvable pane pid,
vanished process, procfs denied) becomes ``status="unavailable"`` with a reason
and ``live=False``, which leaves rule 3b at exactly today's behaviour. Results
are TTL-cached (``liveness.child_proc_probe_ttl_s``, default 5 s) because
``fuse_status`` is a fleet-wide hot read; ``peek()`` is the pure accessor for
payload builders and never scans.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_TTL_S = 5.0
_DEFAULT_SHELL_COMMS = ("bash", "sh", "zsh", "fish", "dash", "ksh", "ash")

# Bound on the /proc scan so a pathological process table can never turn this
# read-path probe into a stall. 20k entries is far above any real host.
_MAX_PROC_ENTRIES = 20000

# F899: the payload cap named by the ruling — at most five comms are carried.
COMM_CAP = 5


@dataclass(frozen=True)
class ChildProbe:
    """One probe outcome. ``live`` is the only thing rule 3b consumes."""

    live: bool
    comms: Tuple[str, ...] = ()
    status: str = "unavailable"  # ok | unavailable
    reason: Optional[str] = None


def _unavailable(reason: str) -> ChildProbe:
    return ChildProbe(live=False, comms=(), status="unavailable", reason=reason)


def _ttl_s() -> float:
    from cli_agent_orchestrator.services.config_service import ConfigService

    try:
        return float(ConfigService.get("liveness.child_proc_probe_ttl_s", _DEFAULT_TTL_S))
    except Exception:
        return _DEFAULT_TTL_S


def _shell_comms() -> frozenset[str]:
    from cli_agent_orchestrator.services.config_service import ConfigService

    try:
        raw = ConfigService.get("liveness.child_proc_shell_comms", None)
    except Exception:
        raw = None
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if parts:
            return frozenset(parts)
    if isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw if str(p).strip()]
        if parts:
            return frozenset(parts)
    return frozenset(_DEFAULT_SHELL_COMMS)


def _scan_proc() -> Tuple[Dict[int, List[int]], Dict[int, str]]:
    """Return (ppid -> [pid], pid -> comm) from one procfs pass.

    Reads ``fork_context_service._PROC_ROOT`` at CALL time so the F124/F139
    synthetic-proc fixture (and tests) can retarget the root. ``/proc/<pid>/stat``
    carries both the comm and the ppid, so this is one read per process.
    """
    from cli_agent_orchestrator.services import fork_context_service as _fcs

    children: Dict[int, List[int]] = {}
    comms: Dict[int, str] = {}
    seen = 0
    for entry in _fcs._PROC_ROOT.iterdir():
        if not entry.name.isdigit():
            continue
        seen += 1
        if seen > _MAX_PROC_ENTRIES:
            break
        try:
            stat = (entry / "stat").read_text()
            close = stat.rfind(")")
            open_paren = stat.find("(")
            comm = stat[open_paren + 1 : close]
            ppid = int(stat[close + 2 :].split()[1])
            pid = int(entry.name)
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(pid)
        comms[pid] = comm
    return children, comms


def _live_work(pane_pid: int) -> ChildProbe:
    """Walk the pane's tree and classify it. Caller wraps; this may raise OSError."""
    children, comms = _scan_proc()
    if pane_pid not in comms:
        return _unavailable("pane_pid_gone")
    shells = _shell_comms()
    base_depth = 1 if comms.get(pane_pid, "") in shells else 0

    work_comms: List[str] = []
    live = False
    # BFS carrying (pid, depth, parent_is_work); `seen` guards a cycle in a
    # malformed/racing snapshot.
    queue: List[Tuple[int, int, bool]] = [(pane_pid, 0, False)]
    seen = {pane_pid}
    while queue:
        pid, depth, parent_is_work = queue.pop(0)
        is_work = False
        if depth > base_depth:
            is_work = parent_is_work or comms.get(pid, "") in shells
            if is_work:
                live = True
                if len(work_comms) < COMM_CAP:
                    work_comms.append(comms.get(pid, ""))
        for child in children.get(pid, ()):
            if child in seen:
                continue
            seen.add(child)
            queue.append((child, depth + 1, is_work))
    return ChildProbe(live=live, comms=tuple(work_comms), status="ok", reason=None)


class ChildProcProbe:
    """TTL-cached, never-raising process-tree probe keyed by terminal id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[float, ChildProbe]] = {}

    def probe(self, terminal_id: str, *, now: Optional[float] = None) -> ChildProbe:
        """Return whether a tool subprocess is alive under this terminal's pane.

        NEVER raises. On any failure the result is ``status="unavailable"`` with
        ``live=False``, so the caller keeps today's behaviour unchanged.
        """
        now = time.monotonic() if now is None else now
        ttl = _ttl_s()
        with self._lock:
            entry = self._cache.get(terminal_id)
            if entry is not None and (now - entry[0]) < ttl:
                return entry[1]

        result = self._probe_uncached(terminal_id)
        with self._lock:
            self._cache[terminal_id] = (now, result)
        return result

    @staticmethod
    def _probe_uncached(terminal_id: str) -> ChildProbe:
        try:
            from cli_agent_orchestrator.clients.database import get_terminal_metadata

            metadata = get_terminal_metadata(terminal_id)
        except Exception:
            return _unavailable("metadata_read_failed")
        if not metadata:
            return _unavailable("no_metadata")
        session = metadata.get("tmux_session")
        window = metadata.get("tmux_window")
        if not session or not window:
            return _unavailable("no_pane_coordinates")

        try:
            from cli_agent_orchestrator.services.fork_context_service import pane_pid

            pid = pane_pid(str(session), str(window))
        except Exception:
            return _unavailable("pane_pid_unresolved")
        if not isinstance(pid, int) or pid <= 0:
            return _unavailable("pane_pid_unresolved")

        try:
            return _live_work(pid)
        except Exception:
            logger.debug("child_proc_probe: /proc walk failed for %s", terminal_id, exc_info=True)
            return _unavailable("procfs_unavailable")

    def peek(self, terminal_id: str) -> Optional[ChildProbe]:
        """Last cached outcome, or None if never probed. Pure — never scans."""
        with self._lock:
            entry = self._cache.get(terminal_id)
        return entry[1] if entry is not None else None

    def forget(self, terminal_id: str) -> None:
        with self._lock:
            self._cache.pop(terminal_id, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


child_proc_probe = ChildProcProbe()
