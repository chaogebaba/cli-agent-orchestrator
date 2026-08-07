"""Offline process, pane, and database identity verification.

The scanner deliberately avoids CAO's HTTP surface: the incident this command
diagnoses is one where that surface may be bound to a stale terminal identity.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from cli_agent_orchestrator.backends.base import PaneIdentityReadResult
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
from cli_agent_orchestrator.utils.tmux_command import tmux_argv

ScanResult = dict[str, Any]
EnvironReader = Callable[[int], dict[str, str]]
ParentReader = Callable[[int], tuple[int | None, str]]
WindowReader = Callable[[str, str], bool]
PaneReader = Callable[[str, str], PaneIdentityReadResult]

_MCP_MODULE = "cli_agent_orchestrator.mcp_server.server"


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
