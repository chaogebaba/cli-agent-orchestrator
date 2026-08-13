"""FX170 — Claude Code session registry reader, target resolver, and socket writer.

Pure read + resolution + socket write. No CAO state imports, no inbox_service
dependency. Testable against a fixture directory and a socketserver stub.

Wire format (probe §2, D5):
  One JSON line, half-close, no read:
  {"msgV":1,"msg_id":"<uuid4>","type":"user",
   "message":{"role":"user","content":"<cross-session-message ...>\\n<text>\\n</cross-session-message>"},
   "priority":"next","from":"bridge:cao-<name>"}

Resolution (D3): pane-pid descent primary, registry `tmux` field cross-check.
Identity guards: procStart PID-reuse check + record freshness.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.services.config_service import ConfigService
from cli_agent_orchestrator.services.fork_context_service import (
    _PROC_ROOT,
    _descendants,
    pane_pid,
)
from cli_agent_orchestrator.utils.tmux_command import tmux_argv

logger = logging.getLogger(__name__)

# D5: sender address must match ^bridge:cao-[A-Za-z0-9._-]{1,64}$
_SENDER_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_SENDER_NAME_LEN = 64

# D6: version band defaults
_DEFAULT_MIN_VERSION = "2.1.0"
_DEFAULT_MAX_VERSION = "2.2.0"

# D3: default max record age
_DEFAULT_MAX_RECORD_AGE_S = 900.0

# D8: default verify timeout
_DEFAULT_VERIFY_TIMEOUT_S = 5.0

# Registry base path
_SESSIONS_DIR = Path.home() / ".claude" / "sessions"


@dataclass
class RegistryRecord:
    """Parsed ~/.claude/sessions/<pid>.json record."""

    pid: int
    session_id: str
    cwd: str
    tmux: str  # "<session>:<window_id>.<pane_id>"
    version: str
    peer_protocol: int
    messaging_socket_path: str
    proc_start: int
    status: str
    status_updated_at: str
    updated_at: str
    raw: dict


@dataclass
class ResolveResult:
    """Outcome of target resolution."""

    record: Optional[RegistryRecord] = None
    refusal_reason: Optional[str] = None


def _sanitize_sender_name(name: str) -> str:
    """D5: sanitize worker name to [A-Za-z0-9._-], truncate to 64."""
    sanitized = _SENDER_SANITIZE_RE.sub("-", name)
    return sanitized[:_MAX_SENDER_NAME_LEN] if sanitized else "unknown"


def _parse_version(ver_str: str) -> Optional[tuple[int, ...]]:
    """Parse a semver-ish version string to a comparable tuple."""
    try:
        parts = ver_str.strip().split(".")
        return tuple(int(p) for p in parts[:3])
    except (ValueError, AttributeError):
        return None


def _read_proc_start(pid: int) -> Optional[int]:
    """Read field 22 (starttime) from /proc/<pid>/stat. Returns None on failure."""
    from cli_agent_orchestrator.services.fork_context_service import _PROC_ROOT as proc_root

    try:
        stat_text = (proc_root / str(pid) / "stat").read_text()
        # Fields after the comm (enclosed in parens)
        tail = stat_text[stat_text.rfind(")") + 2:].split()
        # Field indices: 0=state,1=ppid,...,19=starttime (0-indexed from after comm)
        return int(tail[19])
    except (OSError, ValueError, IndexError):
        return None


def read_registry(sessions_dir: Optional[Path] = None) -> list[RegistryRecord]:
    """Read all valid session records from ~/.claude/sessions/."""
    base = sessions_dir or _SESSIONS_DIR
    records: list[RegistryRecord] = []
    if not base.is_dir():
        return records
    for entry in base.iterdir():
        if not entry.name.endswith(".json"):
            continue
        # Skip .key files — they end with .<hex>.key
        if ".key" in entry.suffixes:
            continue
        # pid.json pattern
        stem = entry.stem
        if not stem.isdigit():
            continue
        try:
            data = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        # Require minimum fields
        if not all(k in data for k in ("sessionId", "messagingSocketPath", "procStart")):
            continue
        # FX170-S2: coerce numeric fields to int at parse time.
        # Claude Code may write procStart/peerProtocol as JSON strings.
        # Fail-closed: unparseable → 0 (guaranteed mismatch against live values).
        try:
            proc_start = int(data.get("procStart", 0))
        except (ValueError, TypeError):
            proc_start = 0
        try:
            peer_protocol = int(data.get("peerProtocol", 0))
        except (ValueError, TypeError):
            peer_protocol = 0

        records.append(RegistryRecord(
            pid=int(stem),
            session_id=data.get("sessionId", ""),
            cwd=data.get("cwd", ""),
            tmux=data.get("tmux", ""),
            version=data.get("version", ""),
            peer_protocol=peer_protocol,
            messaging_socket_path=data.get("messagingSocketPath", ""),
            proc_start=proc_start,
            status=data.get("status", ""),
            status_updated_at=data.get("statusUpdatedAt", ""),
            updated_at=data.get("updatedAt", ""),
            raw=data,
        ))
    return records


def _resolve_tmux_window_id(tmux_session: str, tmux_window_name: str) -> Optional[str]:
    """Resolve a tmux window name to its @id for cross-checking registry tmux field.

    Returns the window_id (e.g. "@0") or None.
    """
    try:
        result = subprocess.run(
            tmux_argv("list-windows", "-t", tmux_session, "-F", "#{window_id} #{window_name}"),
            check=True,
            capture_output=True,
            text=True,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == tmux_window_name:
                return parts[0]
    except (subprocess.CalledProcessError, OSError):
        pass
    return None


def _record_matches_pane(record: RegistryRecord, tmux_session: str, window_id: str) -> bool:
    """Check if a record's tmux field matches the expected pane.

    Registry tmux format: "<session>:<window_id>.<pane_id>"
    """
    if not record.tmux:
        return False
    # Parse: "session_name:@N.%M" or "session_name:@N.M"
    # The window_id in the registry includes the @ prefix
    try:
        colon_idx = record.tmux.index(":")
        rec_session = record.tmux[:colon_idx]
        rest = record.tmux[colon_idx + 1:]
        dot_idx = rest.index(".")
        rec_window_id = rest[:dot_idx]
    except (ValueError, IndexError):
        return False
    return rec_session == tmux_session and rec_window_id == window_id


def resolve_target(
    terminal_id: str,
    tmux_session: str,
    tmux_window: str,
    *,
    sessions_dir: Optional[Path] = None,
    max_record_age_s: Optional[float] = None,
) -> ResolveResult:
    """D3: resolve the CC session registry record for a supervisor terminal.

    Returns ResolveResult with either the record or a refusal reason.
    """
    if max_record_age_s is None:
        max_record_age_s = float(
            ConfigService.get("supervisor.wake.max_record_age_s", default=_DEFAULT_MAX_RECORD_AGE_S)
        )

    records = read_registry(sessions_dir)
    if not records:
        return ResolveResult(refusal_reason="no_registry_records")

    # Step 1: get pane pid and find descendant CC processes
    try:
        pane_leader = pane_pid(tmux_session, tmux_window)
    except (subprocess.CalledProcessError, OSError, ValueError):
        return ResolveResult(refusal_reason="pane_pid_failed")

    descendants = _descendants(pane_leader)
    # Find registry records whose pid is in the descendant tree
    candidate_records = [r for r in records if r.pid in descendants]

    if not candidate_records:
        return ResolveResult(refusal_reason="no_descendant_record")

    # Step 2: resolve the tmux window_id for cross-checking
    window_id = _resolve_tmux_window_id(tmux_session, tmux_window)

    # Step 3: cross-check — prefer records whose tmux field matches this pane
    if window_id is not None:
        matched = [r for r in candidate_records if _record_matches_pane(r, tmux_session, window_id)]
    else:
        # Cannot cross-check — all candidates remain
        matched = candidate_records

    if len(matched) == 0:
        # No cross-check match among candidates — ambiguous
        if len(candidate_records) > 1:
            return ResolveResult(refusal_reason="target_ambiguous")
        # Single candidate without cross-check match — use it (procfs-only resolution)
        matched = candidate_records
    elif len(matched) > 1:
        # Multiple records match the same pane — ambiguous
        return ResolveResult(refusal_reason="target_ambiguous")

    record = matched[0]

    # Guard: procStart PID-reuse check
    live_proc_start = _read_proc_start(record.pid)
    if live_proc_start is None:
        return ResolveResult(refusal_reason="proc_start_unreadable")
    if live_proc_start != record.proc_start:
        return ResolveResult(refusal_reason="proc_start_mismatch")

    # Guard: record freshness
    try:
        from datetime import datetime, timezone
        updated = datetime.fromisoformat(record.updated_at.replace("Z", "+00:00"))
        age_s = (datetime.now(timezone.utc) - updated).total_seconds()
        if age_s > max_record_age_s:
            return ResolveResult(refusal_reason="record_stale")
    except (ValueError, TypeError, AttributeError):
        # If updatedAt is unparseable, treat as stale
        return ResolveResult(refusal_reason="record_stale")

    return ResolveResult(record=record)


def check_version_guard(record: RegistryRecord) -> Optional[str]:
    """D6: version guard. Returns None if OK, or a refusal reason string."""
    # Version must be present
    if not record.version:
        return "version_absent"

    ver = _parse_version(record.version)
    if ver is None:
        return "version_absent"  # unparseable treated same as absent per D6

    min_ver_str = ConfigService.get("supervisor.wake.min_version", default=_DEFAULT_MIN_VERSION)
    max_ver_str = ConfigService.get("supervisor.wake.max_version", default=_DEFAULT_MAX_VERSION)

    min_ver = _parse_version(min_ver_str)
    max_ver = _parse_version(max_ver_str)
    if min_ver is None or max_ver is None:
        return "version_config_invalid"

    if ver < min_ver or ver >= max_ver:
        return "version_out_of_band"

    # peerProtocol check
    if record.peer_protocol != 1:
        return "peer_protocol"

    return None


def build_wake_payload(
    worker_name: str,
    inbox_row_id: int,
    *,
    priority: Optional[str] = None,
) -> str:
    """D5/D7: build the single JSON line for the socket write."""
    if priority is None:
        priority = ConfigService.get("supervisor.wake.priority", default="next")

    sender_name = _sanitize_sender_name(worker_name)
    sender_address = f"bridge:cao-{sender_name}"

    # D7: fixed text, no worker-authored content
    body_text = f"[cao] Callback from {sender_name} (message id {inbox_row_id}). Run any command to surface and ack it."

    # D5: cross-session-message wrapper
    content = (
        f'<cross-session-message from="{sender_address}" from-session="" '
        f'hop-chain="" from-name="cao-{sender_name}" from-mode="bridge">\n'
        f"{body_text}\n"
        f"</cross-session-message>"
    )

    payload = {
        "msgV": 1,
        "msg_id": str(uuid.uuid4()),
        "type": "user",
        "message": {"role": "user", "content": content},
        "priority": priority,
        "from": sender_address,
    }
    return json.dumps(payload, separators=(",", ":"))


def write_to_socket(
    socket_path: str,
    payload_line: str,
    *,
    connect_timeout_s: float = 5.0,
) -> Optional[str]:
    """Write one JSON line to the CC session socket and half-close. No read.

    Returns None on success, or an error reason string on failure.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(connect_timeout_s)
    try:
        sock.connect(socket_path)
        # Write the line + newline, then half-close (D5: no response read)
        data = (payload_line + "\n").encode("utf-8")
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        return None
    except FileNotFoundError:
        return "socket_enoent"
    except ConnectionRefusedError:
        return "socket_econnrefused"
    except PermissionError:
        return "socket_eperm"
    except socket.timeout:
        return "socket_timeout"
    except OSError as e:
        return f"socket_error:{e.errno}"
    finally:
        sock.close()


def verify_wake(
    record: RegistryRecord,
    pre_status_updated_at: str,
    *,
    sessions_dir: Optional[Path] = None,
    timeout_s: Optional[float] = None,
) -> bool:
    """D8: verify that the target woke by polling its registry record.

    Returns True if statusUpdatedAt advanced or status became busy
    (with statusUpdatedAt advancing past pre-sample).
    """
    if timeout_s is None:
        timeout_s = float(
            ConfigService.get("supervisor.wake.verify_timeout_s", default=_DEFAULT_VERIFY_TIMEOUT_S)
        )

    base = sessions_dir or _SESSIONS_DIR
    record_path = base / f"{record.pid}.json"
    deadline = time.monotonic() + timeout_s
    poll_interval = 0.5

    while time.monotonic() < deadline:
        try:
            data = json.loads(record_path.read_text())
            current_status_updated = data.get("statusUpdatedAt", "")
            if current_status_updated and current_status_updated != pre_status_updated_at:
                return True
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(poll_interval)

    return False
