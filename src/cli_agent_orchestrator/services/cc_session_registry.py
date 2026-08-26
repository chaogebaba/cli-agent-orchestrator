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
from cli_agent_orchestrator.utils.provider_plane import provider_home
from cli_agent_orchestrator.utils.sandbox_guard import SandboxProviderUnsafe
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

# F337 B1: canonical default for supervisor.wake.native — ship DARK (False).
# All call sites and the config-registry entry MUST reference this constant.
WAKE_NATIVE_DEFAULT = False


# Registry base path — resolved per call through the injected provider plane, never
# from Path.home() directly: cao-server runs OUTSIDE the worker's bwrap, so a literal
# ~/.claude would make a sandboxed instance read the operator's real session registry
# and write to the operator's live Claude Code sockets (g7b native-home guard).
def _sessions_dir() -> Optional[Path]:
    """Injected ~/.claude/sessions equivalent, or None when the plane is unusable."""
    try:
        return provider_home("claude_code").sessions
    except SandboxProviderUnsafe as exc:
        # Fail closed: callers degrade to the nudge transport. Log so the operator can
        # tell an unusable plane apart from a genuinely empty registry / unverified wake.
        logger.warning("f170_doorbell registry_plane_unusable reason=%s", exc)
        return None


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


def _parse_registry_timestamp(value) -> str:
    """Normalize a registry timestamp to ISO8601 string.

    Claude Code writes updatedAt/statusUpdatedAt as either:
      - ISO8601 string (e.g. "2026-08-13T12:00:00+00:00")
      - Epoch-milliseconds integer (e.g. 1786649639899)
      - Epoch-seconds integer (e.g. 1786649639)
      - Numeric string variants of the above

    Threshold heuristic: numeric values >= 1e12 (1_000_000_000_000) are treated
    as epoch-milliseconds; values < 1e12 are treated as epoch-seconds. This
    threshold corresponds to 2001-09-09T01:46:40Z in epoch-ms (well before any
    plausible CC session) and 33658-09-27 in epoch-seconds (well beyond any
    plausible session), so there is no ambiguity for real-world timestamps.

    Returns an ISO8601 string suitable for datetime.fromisoformat().
    Returns "" on unparseable input (fail-closed: empty → treated as stale).
    """
    from datetime import datetime, timezone

    def _numeric_to_iso(num: float) -> str:
        """Convert a numeric timestamp to ISO8601 using the 1e12 threshold."""
        try:
            if num >= 1e12:
                # Epoch-milliseconds
                epoch_s = num / 1000.0
            else:
                # Epoch-seconds
                epoch_s = num
            return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return ""

    if isinstance(value, str):
        # Try numeric string first
        stripped = value.strip()
        if stripped.isdigit() and len(stripped) >= 10:
            return _numeric_to_iso(int(stripped)) or ""
        # Otherwise treat as ISO8601 — return as-is (validated downstream)
        return value if value else ""
    elif isinstance(value, (int, float)):
        return _numeric_to_iso(value) or ""
    return ""


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
    base = sessions_dir or _sessions_dir()
    records: list[RegistryRecord] = []
    if base is None or not base.is_dir():
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
        # D13: Require minimum fields — messagingSocketPath is optional
        # (CC 2.1.232 dropped the field); sessionId + procStart suffice.
        if not all(k in data for k in ("sessionId", "procStart")):
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

        # FX179: coerce timestamp fields at parse time.
        # Claude Code may write updatedAt/statusUpdatedAt as epoch-ms integers.
        # Normalize to ISO8601 string for consistent downstream handling.
        updated_at = _parse_registry_timestamp(data.get("updatedAt", ""))
        status_updated_at = _parse_registry_timestamp(data.get("statusUpdatedAt", ""))

        # F216: coerce explicit JSON null → "" for all string fields.
        # dict.get(key, default) returns None when the key IS present with value
        # null; the `or ""` pattern normalizes that to empty-string at parse time.
        records.append(RegistryRecord(
            pid=int(stem),
            session_id=data.get("sessionId", "") or "",
            cwd=data.get("cwd", "") or "",
            tmux=data.get("tmux", "") or "",
            version=data.get("version", "") or "",
            peer_protocol=peer_protocol,
            messaging_socket_path=data.get("messagingSocketPath", "") or "",
            proc_start=proc_start,
            status=data.get("status", "") or "",
            status_updated_at=status_updated_at,
            updated_at=updated_at,
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
        if age_s < 0 or age_s > max_record_age_s:
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


# F459: Max message body bytes embedded in the native bridge message.
_F459_MAX_BODY_BYTES = 8192


def build_wake_payload(
    worker_name: str,
    inbox_row_id: int,
    *,
    priority: Optional[str] = None,
    message_body: Optional[str] = None,
    sender_display_name: Optional[str] = None,
) -> str:
    """D5/D7/F459: build the single JSON line for the socket write.

    F459: When message_body is provided, the bridge message carries the actual
    worker callback text (defensively truncated to 8KB) instead of a generic ping.
    sender_display_name overrides the from-name (the worker's display name).
    """
    if priority is None:
        priority = ConfigService.get("supervisor.wake.priority", default="next")

    sender_name = _sanitize_sender_name(worker_name)
    sender_address = f"bridge:cao-{sender_name}"

    # F459: from-name = worker display name (not "cao-" prefixed)
    from_name = _sanitize_sender_name(sender_display_name) if sender_display_name else sender_name

    # F459: payload-carrying body (or legacy fixed text for fallback callers)
    if message_body is not None:
        # Defensive truncation to 8KB with tail pointer
        if len(message_body) > _F459_MAX_BODY_BYTES:
            body_text = (
                message_body[:_F459_MAX_BODY_BYTES]
                + f"\n\n[truncated — full text: inbox row {inbox_row_id}]"
            )
        else:
            body_text = message_body
    else:
        # Legacy: no content provided — generic ping (pre-F459 callers)
        body_text = (
            f"[cao] Callback from {from_name} (message id {inbox_row_id}). "
            f"Run any command to surface and ack it."
        )

    # F459: summary = first line (for collapsed render)
    summary_line = body_text.split("\n", 1)[0][:120]

    # D5: cross-session-message wrapper
    content = (
        f'<cross-session-message from="{sender_address}" from-session="" '
        f'hop-chain="" from-name="{from_name}" from-mode="bridge" '
        f'summary="{summary_line}">\n'
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


def read_peer_token(
    pid: int,
    sessions_dir: Optional[Path] = None,
    *,
    expected_proc_start: Optional[int] = None,
) -> Optional[str]:
    """F337: Read the peerToken from the session key file for a given PID.

    Key files are at <sessions_dir>/<pid>.<64hex>.key and contain JSON:
    {"peerToken":"<hex>","procStart":"<int>"}.

    F337-r2 B2: When expected_proc_start is provided, the key file's procStart
    MUST match the live process incarnation. A mismatch means a stale key from
    a recycled PID — returns None (clean fallback, no auth frame sent).

    F337-r3 B1: Exactly one valid candidate must exist. Multiple valid key files
    for the same PID (ambiguity) return None — directory order is not an identity
    rule, so a stale/rotated key must never be selected arbitrarily.

    F337-r3 S1: Directory enumeration errors (OSError) return None cleanly.

    Returns the peerToken string, or None if no key file exists, is unreadable,
    malformed, ambiguous, or the procStart identity check fails.
    """
    base = sessions_dir or _sessions_dir()
    if base is None or not base.is_dir():
        return None
    # F337-r2 B2: strict filename — <pid>.<64hex>.key only
    _KEY_PATTERN = re.compile(rf"^{pid}\.[0-9a-fA-F]{{64}}\.key$")
    # F337-r3 B1: collect ALL valid candidates, require exactly one
    candidates: list[str] = []
    # F337-r3 S1: wrap iterdir() so unreadable directory degrades cleanly
    try:
        entries = list(base.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not _KEY_PATTERN.match(entry.name):
            continue
        try:
            data = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        # F337-r2 S1: non-object JSON → malformed → skip
        if not isinstance(data, dict):
            continue
        # F337-r2 B2: verify procStart matches the live process incarnation
        if expected_proc_start is not None:
            try:
                key_proc_start = int(data.get("procStart", -1))
            except (ValueError, TypeError):
                # Unparseable procStart → treat as mismatch
                continue
            if key_proc_start != expected_proc_start:
                logger.debug(
                    "f337_peer_token_procstart_mismatch pid=%s "
                    "key_procstart=%s expected=%s",
                    pid,
                    data.get("procStart"),
                    expected_proc_start,
                )
                continue
        token = data.get("peerToken")
        if token and isinstance(token, str):
            candidates.append(token)
    # F337-r3 B1: ambiguity → None (fail closed before write_to_socket)
    if len(candidates) != 1:
        if len(candidates) > 1:
            logger.debug(
                "f337_peer_token_ambiguous pid=%s candidate_count=%d",
                pid,
                len(candidates),
            )
        return None
    return candidates[0]


def _build_auth_frame(token: str) -> str:
    """F337: Build the JSON auth handshake line for CC messaging UDS.

    Wire format (probed against CC 2.1.246):
      {"type":"auth","token":"<peerToken>"}\n
    Sent as the first line before the message payload.
    """
    return json.dumps({"type": "auth", "token": token}, separators=(",", ":"))


def write_to_socket(
    socket_path: str,
    payload_line: str,
    *,
    connect_timeout_s: float = 5.0,
    auth_token: Optional[str] = None,
) -> Optional[str]:
    """Write one JSON line to the CC session socket and half-close. No read.

    F337: When auth_token is provided, sends a JSON auth frame as the first line
    before the payload: {"type":"auth","token":"<token>"}\n
    This authenticates the connection using the per-session peerToken from the
    key file at ~/.claude/sessions/<pid>.<hex>.key.

    Returns None on success, or an error reason string on failure.
    """
    # F216: EINVAL short-circuit — refuse empty/null socket path before connect
    if not socket_path:
        return "socket_path_empty"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(connect_timeout_s)
    try:
        sock.connect(socket_path)
        # F337: Auth handshake — JSON frame as first line
        if auth_token:
            auth_frame = _build_auth_frame(auth_token) + "\n"
            sock.sendall(auth_frame.encode("utf-8"))
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
        if e.errno == 22:  # EINVAL
            return "socket_einval"
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

    base = sessions_dir or _sessions_dir()
    if base is None:
        return False
    record_path = base / f"{record.pid}.json"
    deadline = time.monotonic() + timeout_s
    poll_interval = 0.5

    while time.monotonic() < deadline:
        try:
            data = json.loads(record_path.read_text())
            # FX179: normalize — raw JSON may contain epoch-ms int
            current_status_updated = _parse_registry_timestamp(data.get("statusUpdatedAt", ""))
            if current_status_updated and current_status_updated != pre_status_updated_at:
                return True
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(poll_interval)

    return False
