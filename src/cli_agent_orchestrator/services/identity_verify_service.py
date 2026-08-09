"""Offline process, pane, and database identity verification.

The scanner deliberately avoids CAO's HTTP surface: the incident this command
diagnoses is one where that surface may be bound to a stale terminal identity.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from cli_agent_orchestrator.backends.base import PaneIdentityReadResult
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
from cli_agent_orchestrator.constants import DATABASE_FILE
from cli_agent_orchestrator.utils.tmux_command import tmux_argv

ScanResult = dict[str, Any]
EnvironReader = Callable[[int], dict[str, str]]
ParentReader = Callable[[int], tuple[int | None, str]]
WindowReader = Callable[[str, str], bool]
PaneReader = Callable[[str, str], PaneIdentityReadResult]

_MCP_MODULE = "cli_agent_orchestrator.mcp_server.server"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcSnapshot:
    """The process fields needed to identify an MCP carrier."""

    pid: int
    comm: str
    argv: list[str]


def _read_argv(pid: int) -> list[str]:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    return [item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item]


def _live_processes() -> list[ProcSnapshot]:
    processes: list[ProcSnapshot] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
            argv = _read_argv(pid)
        except (FileNotFoundError, ProcessLookupError, PermissionError, UnicodeError, OSError):
            continue
        processes.append(ProcSnapshot(pid=pid, comm=comm, argv=argv))
    return processes


def _is_carrier(process: ProcSnapshot) -> bool:
    name_match = process.comm == "cao-mcp-server"
    module_match = any(
        process.argv[index] == "-m" and process.argv[index + 1] == _MCP_MODULE
        for index in range(len(process.argv) - 1)
    )
    return name_match or module_match


def read_process_environ(pid: int) -> dict[str, str]:
    """Read one process environment from procfs."""

    result: dict[str, str] = {}
    for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        try:
            result[key.decode("utf-8")] = value.decode("utf-8")
        except UnicodeError:
            continue
    return result


def _read_carrier_status(pid: int) -> int:
    """Return the parent pid from a carrier's procfs status record."""

    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("PPid:"):
            return int(line.split(":", 1)[1].strip())
    raise ValueError(f"PPid missing from /proc/{pid}/status")


def _read_parent_cmdline(ppid: int) -> str:
    """Return a readable command line for a carrier's parent process."""

    raw = Path(f"/proc/{ppid}/cmdline").read_bytes()
    return " ".join(item.decode("utf-8") for item in raw.split(b"\0") if item)


def read_parent(pid: int) -> tuple[int | None, str]:
    """Read parent provenance while preserving carrier-loss provenance.

    Loss of the carrier status propagates.  Once that status has supplied a
    parent pid, loss of the parent itself converts to the documented unknown
    parent value instead.
    """

    ppid = _read_carrier_status(pid)
    if ppid <= 0:
        return None, ""
    try:
        return ppid, _read_parent_cmdline(ppid)
    except (FileNotFoundError, ProcessLookupError, UnicodeDecodeError):
        return None, ""


def _window_is_live(session_name: str, window_name: str) -> bool:
    result = subprocess.run(
        tmux_argv("list-panes", "-t", f"{session_name}:{window_name}", "-F", "#{pane_id}"),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _normalize_endpoint(endpoint: str) -> str:
    return endpoint[:-1] if endpoint.endswith("/") else endpoint


def _format_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _default_endpoint(environ: dict[str, str]) -> str:
    host = environ.get("CAO_API_HOST") or "127.0.0.1"
    port = environ.get("CAO_API_PORT") or "9889"
    return f"http://{_format_host(host)}:{port}"


def _parent_kind(cmdline: str) -> str:
    if "bg-spare" in cmdline:
        return "bg-spare"
    tokens = cmdline.split()
    first = Path(tokens[0]).name if tokens else ""
    if first == "claude" or cmdline.startswith("claude "):
        return "claude"
    if first == "grok" or "/grok " in cmdline:
        return "grok"
    if first == "codex" or "/codex " in cmdline:
        return "codex"
    return "other"


def _read_terminal_rows(db_path: Path) -> list[dict[str, Any]]:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, tmux_session, tmux_window, agent_profile FROM terminals"
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _is_self_or_ancestor(pid: int) -> bool:
    """True when ``pid`` is this process or an ancestor of it (Probe-10 self-proof).

    Walks the ppid chain upward from ``os.getpid()`` reusing this module's
    ``_read_carrier_status`` (/proc/{pid}/status → ``PPid:``), stopping at a
    match or pid ≤ 1. O(ancestry depth) — do NOT use
    ``fork_context_service._descendants`` which is O(all processes).
    """

    current = os.getpid()
    while current > 1:
        if current == pid:
            return True
        try:
            current = _read_carrier_status(current)
        except (FileNotFoundError, ProcessLookupError, ValueError, OSError):
            return False
    return current == pid


def _resolve_pane_window(pane_id: str) -> tuple[str, str] | None:
    """Resolve a tmux pane id to its CURRENT ``session:window`` via tmux.

    Uses the single-command form ``display-message -p -t %N`` verified live by
    the F99 gates (NOT a list-panes membership scan). Returns ``None`` on any
    resolution failure (tmux absent, pane gone, command error).
    """

    try:
        result = subprocess.run(
            tmux_argv(
                "display-message", "-p", "-t", f"%{pane_id}", "#{session_name}:#{window_name}"
            ),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError) as exc:
        logger.warning("diagnose_own_terminal: tmux display-message failed: %s", exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "diagnose_own_terminal: tmux display-message rc=%s stderr=%s",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return None
    text = (result.stdout or "").strip()
    if not text or ":" not in text:
        return None
    session_name, window_name = text.split(":", 1)
    return session_name, window_name


# Failed-resolution cache: positive-only, 60s TTL, keyed by own_id.
# A single 404 → diagnosis fires a tmux subprocess; the TTL collapses a retry
# storm to one probe. Negative/indeterminate outcomes are NOT cached so a
# recovering tmux is re-probed on the next 404.
_DIAG_CACHE_TTL_S = 60.0
_diag_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def diagnose_own_terminal(
    own_id: str,
    *,
    db_path: Path | None = None,
    pane_pid_self: Callable[[int], bool] = _is_self_or_ancestor,
) -> dict[str, Any]:
    """Single-caller diagnosis for a 404 on the caller's OWN terminal id.

    Reuses this module's pane/DB primitives (read_pane_identity,
    _read_terminal_rows). Returns a dict with the two-branch verdict:
    ``row_gone`` (PASS — caller alive in its own pane, DB row gone),
    ``ambiguous`` (≥2 rows claim the window), ``self_proof_fail``,
    ``no_pane``. Positive outcomes are cached for 60s keyed by ``own_id``.
    """

    try:
        pane_id = os.environ["TMUX_PANE"]
    except KeyError:
        return {"branch": "no_pane"}

    now = time.monotonic()
    cached = _diag_cache.get(own_id)
    if cached is not None and now - cached[0] < _DIAG_CACHE_TTL_S:
        return cached[1]

    resolved = _resolve_pane_window(pane_id)
    if resolved is None:
        return {"branch": "no_pane"}

    session_name, window_name = resolved
    try:
        pane_pids = TmuxBackend()._pane_pids(session_name, window_name)
    except (OSError, ValueError) as exc:
        logger.warning("diagnose_own_terminal: pane_pid read failed: %s", exc)
        return {"branch": "self_proof_fail", "session": session_name, "window": window_name}
    if len(pane_pids) != 1:
        return {"branch": "self_proof_fail", "session": session_name, "window": window_name}
    pane_pid_value = pane_pids[0]

    # Probe-10 self-proof: accept the pane ONLY if its pid is this process or
    # an ancestor. Otherwise the pane is someone else's (stale TMUX_PANE after
    # a tmux server restart) and the diagnosis is not trusted.
    if not pane_pid_self(pane_pid_value):
        return {
            "branch": "self_proof_fail",
            "session": session_name,
            "window": window_name,
            "pane_pid": pane_pid_value,
        }

    terminal_rows = _read_terminal_rows(db_path or DATABASE_FILE)
    db_matches = sorted(
        str(row["id"])
        for row in terminal_rows
        if str(row["tmux_session"]) == session_name and str(row["tmux_window"]) == window_name
    )

    result: dict[str, Any]
    if len(db_matches) > 1:
        result = {
            "branch": "ambiguous",
            "session": session_name,
            "window": window_name,
            "pane_pid": pane_pid_value,
            "db_matches": db_matches,
        }
    else:
        result = {
            "branch": "row_gone",
            "session": session_name,
            "window": window_name,
            "pane_pid": pane_pid_value,
            "db_matches": db_matches,
        }

    _diag_cache[own_id] = (now, result)
    return result


def scan_identity(
    *,
    endpoint: str,
    db_path: Path,
    processes: Sequence[ProcSnapshot] | None = None,
    process_environ_reader: EnvironReader = read_process_environ,
    parent_reader: ParentReader = read_parent,
    window_reader: WindowReader = _window_is_live,
    pane_reader: PaneReader | None = None,
) -> ScanResult:
    """Return the offline identity scan document specified by F94R."""

    scan_endpoint = _normalize_endpoint(endpoint)
    terminal_rows = _read_terminal_rows(db_path)
    terminals_by_id = {str(row["id"]): row for row in terminal_rows}
    source_processes = _live_processes() if processes is None else processes
    candidates = sorted(
        {process.pid: process for process in source_processes if _is_carrier(process)}.values(),
        key=lambda process: process.pid,
    )
    if pane_reader is None:
        pane_reader = TmuxBackend().read_pane_identity

    rows: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
    vanished_pids: list[int] = []
    window_cache: dict[tuple[str, str], bool] = {}

    def window_live(session_name: str, window_name: str) -> bool:
        key = (session_name, window_name)
        if key not in window_cache:
            window_cache[key] = bool(window_reader(session_name, window_name))
        return window_cache[key]

    for process in candidates:
        try:
            environ = process_environ_reader(process.pid)
        except (FileNotFoundError, ProcessLookupError):
            vanished_pids.append(process.pid)
            continue
        except OSError:
            environ = {}

        explicit_endpoint = environ.get("CAO_ENDPOINT") or ""
        effective_endpoint = _normalize_endpoint(
            explicit_endpoint if explicit_endpoint else _default_endpoint(environ)
        )
        endpoint_source = "env" if explicit_endpoint else "defaulted"

        try:
            parent_pid, parent_cmd = parent_reader(process.pid)
        except (FileNotFoundError, ProcessLookupError):
            vanished_pids.append(process.pid)
            continue
        parent_kind = _parent_kind(parent_cmd)
        parent_cmd = parent_cmd[:200]
        mcp_tid = environ.get("CAO_TERMINAL_ID") or None

        if effective_endpoint != scan_endpoint:
            out_of_scope.append(
                {
                    "mcp_pid": process.pid,
                    "mcp_tid": mcp_tid,
                    "endpoint": effective_endpoint,
                    "reason": "endpoint_mismatch",
                    "parent_cmd": parent_cmd,
                }
            )
            continue

        db_row = terminals_by_id.get(mcp_tid or "")
        tid_in_db = db_row is not None and mcp_tid is not None
        is_window_live: bool | None = None
        pane_tid: str | None = None
        pane_reason: str | None = None
        pane_agrees: bool | None = None

        if tid_in_db and db_row is not None:
            is_window_live = window_live(str(db_row["tmux_session"]), str(db_row["tmux_window"]))
            if is_window_live:
                pane_read = pane_reader(str(db_row["tmux_session"]), str(db_row["tmux_window"]))
                pane_tid = pane_read.identity
                pane_reason = pane_read.reason
                pane_agrees = pane_tid == mcp_tid if pane_tid is not None else None

        fail_reasons: list[str] = []
        if mcp_tid is None:
            fail_reasons.append("missing_mcp_tid")
        elif not tid_in_db:
            fail_reasons.append("tid_not_in_db")
        if pane_tid is not None and pane_tid != mcp_tid:
            fail_reasons.append("pane_tid_mismatch")

        if fail_reasons:
            verdict = "FAIL"
        elif pane_reason is not None or is_window_live is False:
            verdict = "WARN"
        else:
            verdict = "OK"

        rows.append(
            {
                "mcp_pid": process.pid,
                "mcp_tid": mcp_tid,
                "endpoint": effective_endpoint,
                "endpoint_source": endpoint_source,
                "parent_pid": parent_pid,
                "parent_cmd": parent_cmd,
                "parent_kind": parent_kind,
                "tid_in_db": tid_in_db,
                "db": db_row,
                "window_live": is_window_live,
                "pane_tid": pane_tid,
                "pane_reason": pane_reason,
                "pane_agrees": pane_agrees,
                "verdict": verdict,
                "fail_reasons": fail_reasons,
            }
        )

    rows.sort(key=lambda row: (row["mcp_tid"] or "", row["mcp_pid"]))
    out_of_scope.sort(key=lambda row: row["mcp_pid"])
    vanished_pids.sort()

    authority: list[dict[str, Any]] = []
    authority_by_window: dict[tuple[str, str], list[str]] = defaultdict(list)
    for terminal in terminal_rows:
        session_name = str(terminal["tmux_session"])
        window_name = str(terminal["tmux_window"])
        if window_live(session_name, window_name):
            authority_by_window[(session_name, window_name)].append(str(terminal["id"]))
    summary_warn: list[str] = []
    for (session_name, window_name), db_ids in sorted(authority_by_window.items()):
        ids = sorted(db_ids)
        authority.append({"tmux_session": session_name, "tmux_window": window_name, "db_ids": ids})
        if len(ids) > 1:
            summary_warn.append(
                f"multiple DB identities claim live window {session_name}:{window_name}: "
                + ",".join(ids)
            )

    verdict_counts = Counter(row["verdict"] for row in rows)
    scan_warning: str | None = None
    if not rows and out_of_scope:
        scan_warning = (
            f"{len(out_of_scope)} carriers found, none matched scan endpoint {scan_endpoint}"
        )
        summary_warn.append(scan_warning)

    return {
        "scan_endpoint": scan_endpoint,
        "scan_db": str(db_path.resolve()),
        "rows": rows,
        "out_of_scope": out_of_scope,
        "vanished_pids": vanished_pids,
        "summary": {
            "in_scope": len(rows),
            "ok": verdict_counts["OK"],
            "warn": verdict_counts["WARN"],
            "fail": verdict_counts["FAIL"],
            "out_of_scope": len(out_of_scope),
            "vanished": len(vanished_pids),
            "scan_warning": scan_warning,
        },
        "summary_warn": summary_warn,
        "window_authority": authority,
    }
