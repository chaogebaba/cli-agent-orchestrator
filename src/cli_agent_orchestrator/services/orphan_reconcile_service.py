"""F138: Incarnation-token orphan process reconciliation.

After CAO confirms that a terminal incarnation has lost its tmux window,
no process belonging to that incarnation may remain alive outside CAO control.
This module owns the durable job dispatcher, procfs token scan, pidfd-based
signaling, and fixed-point TERM→KILL termination loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
import struct
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# --- Constants ----------------------------------------------------------------

ORPHAN_TERM_GRACE_SECONDS: float = 5.0
INCARNATION_LAUNCH_STALE_SECONDS: float = 120.0
_RESCAN_INTERVAL_S: float = 0.25
_POST_KILL_ROUNDS: int = 3
_DISPATCHER_POLL_S: float = 30.0
_MAX_CONCURRENT_JOBS: int = 2
_BATCH_CLAIM_CAP: int = 10
_LEASE_DURATION_S: float = 30.0
_RETRY_DELAYS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
_PROC_ROOT = Path("/proc")
_EMPTY_SCAN_CONFIRM_COUNT: int = 2

# F166 D7: bounded daemon notification
MAX_NOTIFICATIONS_PER_JOB: int = 3

# F166-F1: failure codes known to be permanent (condition cannot resolve without
# external intervention). Fast-track to attention_required on first observation
# instead of burning through all retry delays.
_PERMANENT_FAILURE_PREFIXES: frozenset[str] = frozenset(
    {
        "permission_denied_server_ancestor",
        "permission_denied_uid_unknown",
    }
)

# Safety: never signal these
_PROTECTED_PIDS: frozenset[int] = frozenset({0, 1})


def _is_permanent_failure(detail: str | None) -> bool:
    """F166-F1: Return True if ALL errors in the scan detail are known-permanent.

    A scan_incomplete whose errors are exclusively from _PERMANENT_FAILURE_PREFIXES
    will never resolve without external intervention (e.g., rebooting, changing
    permissions). Retrying is wasteful and delays the attention notification.
    """
    if not detail:
        return False
    # detail is a semicolon-separated list of error annotations (up to 3)
    errors = [e.strip() for e in detail.split(";") if e.strip()]
    if not errors:
        return False
    return all(
        any(err.startswith(prefix) for prefix in _PERMANENT_FAILURE_PREFIXES) for err in errors
    )


# --- Data classes -------------------------------------------------------------


@dataclass(frozen=True)
class ProcessIncarnation:
    id: str
    terminal_id: str
    terminal_generation: int
    token: str
    token_hash: str
    owner_uid: int
    provider: str
    pane_pid: int | None
    pane_start_ticks: int | None
    state: str
    created_at: datetime
    activated_at: datetime | None = None
    reconciled_at: datetime | None = None


@dataclass(frozen=True)
class ProcTokenMatch:
    pid: int
    start_ticks: int
    uid: int


@dataclass(frozen=True)
class ProcScanResult:
    matches: tuple[ProcTokenMatch, ...]
    complete: bool
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SignalResult:
    signaled: int
    failed: int
    safety_aborts: int


@dataclass(frozen=True)
class ReconcileAttemptResult:
    code: str
    complete_scan: bool
    scanned: int
    term_signaled: int
    kill_signaled: int
    residual: int
    retry_delay_s: float | None
    detail: str | None


@dataclass(frozen=True)
class LivenessObservationResult:
    job_queued: bool
    confirmation_count: int
    detail: str | None = None


@dataclass(frozen=True)
class JobRequestResult:
    created: bool
    job_id: str | None
    detail: str | None = None


# --- Token helpers ------------------------------------------------------------


def generate_incarnation_token() -> str:
    """Generate a 192-bit cryptographically random token (base16, 48 chars)."""
    return os.urandom(24).hex()


def hash_token(token: str) -> str:
    """Irreversible short hash for logs. Never log the raw token."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()[:16]


# --- Procfs token scan --------------------------------------------------------


def _read_proc_start_ticks(pid: int) -> int | None:
    """Read field 22 (starttime) from /proc/<pid>/stat as integer clock ticks."""
    try:
        stat_data = (_PROC_ROOT / str(pid) / "stat").read_text()
        # Field 22 is after the comm field (which may contain spaces/parens)
        # Find the last ')' which ends the comm field
        comm_end = stat_data.rfind(")")
        if comm_end < 0:
            return None
        fields = stat_data[comm_end + 2 :].split()
        # Fields after comm: state(0), ppid(1), pgrp(2), session(3), ...
        # starttime is field index 19 (0-based after state)
        if len(fields) > 19:
            return int(fields[19])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _read_proc_uid(pid: int) -> int | None:
    """Read real UID from /proc/<pid>/status."""
    try:
        for line in (_PROC_ROOT / str(pid) / "status").open():
            if line.startswith("Uid:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1])
    except (OSError, ValueError):
        pass
    return None


def scan_incarnation_processes(
    token: str,
    owner_uid: int,
    *,
    issuance_ticks: int | None = None,
    issuance_boot_id: str | None = None,
) -> ProcScanResult:
    """Scan all /proc entries for exact NUL-delimited token match.

    A process belongs to an incarnation only when one complete entry equals:
        b"CAO_PROCESS_INCARNATION=" + token.encode("ascii")

    Returns typed evidence per PID. Token bytes and other env vars are never logged.
    """
    target = b"CAO_PROCESS_INCARNATION=" + token.encode("ascii")
    matches: list[ProcTokenMatch] = []
    errors: list[str] = []
    complete = True

    try:
        proc_entries = list(_PROC_ROOT.iterdir())
    except OSError as e:
        return ProcScanResult(matches=(), complete=False, errors=[f"procfs_unavailable: {e}"])

    # D17: Read current boot_id once for issuance fence comparisons
    current_boot_id = _read_boot_id()

    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in _PROTECTED_PIDS:
            continue

        environ_path = entry / "environ"
        try:
            environ_data = environ_path.read_bytes()
        except PermissionError:
            # D16: Full decision tree for unreadable environ.
            proc_uid = _read_proc_uid(pid)
            if proc_uid is None:
                # UID unprovable → fail closed (could be same-UID with token)
                complete = False
                errors.append(f"permission_denied_uid_unknown:pid={pid}")
                continue
            if proc_uid != owner_uid:
                # Foreign UID — benign, cannot carry our token
                continue
            # Same UID — check if server/ancestor (which cannot carry token)
            try:
                is_ancestor = _is_server_or_ancestor(pid)
            except Exception:
                # Ancestry proof failed → fail closed
                complete = False
                errors.append(f"permission_denied_same_uid_ancestry_failed:pid={pid}")
                continue
            if is_ancestor:
                # Verified server ancestor: cannot carry per-terminal token
                # (token is injected into descendant pane, never flows up).
                # Annotate but preserve completeness.
                errors.append(f"permission_denied_server_ancestor:pid={pid}")
                continue
            # Same UID, not ancestor — D17 pre-issuance fence check
            if (
                issuance_boot_id is not None
                and current_boot_id is not None
                and current_boot_id == issuance_boot_id
            ):
                # Same boot — check if this process predates token issuance
                proc_start = _read_proc_start_ticks(pid)
                if (
                    proc_start is not None
                    and issuance_ticks is not None
                    and proc_start < issuance_ticks
                ):
                    # Process started before token was issued → cannot carry it
                    errors.append(f"predates_issuance:pid={pid}")
                    continue
            # F465: Before failing closed, check if this process is NOT a
            # descendant of the server. Incarnation tokens are inherited via
            # env from the spawned tmux pane — only server descendants can
            # carry them. User-session processes (systemd --user children,
            # sd-pam, etc.) that are same-UID but not in the server's subtree
            # are benign and must not cause scan_incomplete false-alarms.
            try:
                is_descendant = _is_descendant_of_server(pid)
            except Exception:
                is_descendant = True  # fail closed if ancestry walk fails
            if not is_descendant:
                errors.append(f"not_server_descendant:pid={pid}")
                continue
            # Fall through: genuinely unreadable, fail closed
            complete = False
            errors.append(f"permission_denied_same_uid:pid={pid}")
            continue
        except OSError:
            # Process may have exited between listing and reading
            continue

        # Exact NUL-delimited matching — no substring
        env_entries = environ_data.split(b"\x00")
        if target in env_entries:
            # Verify UID
            proc_uid = _read_proc_uid(pid)
            if proc_uid != owner_uid:
                continue
            start_ticks = _read_proc_start_ticks(pid)
            if start_ticks is None:
                errors.append(f"no_start_ticks:pid={pid}")
                continue
            matches.append(ProcTokenMatch(pid=pid, start_ticks=start_ticks, uid=proc_uid))

    return ProcScanResult(matches=tuple(matches), complete=complete, errors=errors)


# --- pidfd signal authority ---------------------------------------------------


def _read_boot_id() -> str | None:
    """Read /proc/sys/kernel/random/boot_id. Returns stripped string or None."""
    try:
        return (_PROC_ROOT / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
    except OSError:
        return None


def _is_server_or_ancestor(pid: int) -> bool:
    """Check if pid is the current server process or one of its ancestors."""
    server_pid = os.getpid()
    if pid == server_pid:
        return True
    # Walk parent chain
    current = server_pid
    while current > 1:
        try:
            stat_data = (_PROC_ROOT / str(current) / "stat").read_text()
            comm_end = stat_data.rfind(")")
            if comm_end < 0:
                break
            fields = stat_data[comm_end + 2 :].split()
            ppid = int(fields[1])  # ppid is field index 1 after state
            if ppid == pid:
                return True
            current = ppid
        except (OSError, ValueError, IndexError):
            break
    return False


def _is_descendant_of_server(pid: int) -> bool:
    """F465: Check if pid is a descendant of the current server process.

    Walks the parent chain of `pid` upward. Returns True if the server PID
    is found in the ancestry before reaching PID 1. Only processes that are
    descendants of the server can carry an incarnation token (inherited via
    env from the spawned tmux pane).

    Fails closed: if the parent chain cannot be fully walked (missing /proc
    entries), returns True (assume potentially dangerous) to preserve
    scan_incomplete semantics for unresolvable cases.
    """
    server_pid = os.getpid()
    if pid == server_pid:
        return False  # the server itself is not its own descendant
    current = pid
    visited: set[int] = set()
    while current > 1:
        if current in visited:
            return True  # cycle → fail closed
        visited.add(current)
        try:
            stat_data = (_PROC_ROOT / str(current) / "stat").read_text()
            comm_end = stat_data.rfind(")")
            if comm_end < 0:
                return True  # unparseable → fail closed
            fields = stat_data[comm_end + 2 :].split()
            ppid = int(fields[1])  # ppid is field index 1 after state
        except (OSError, ValueError, IndexError):
            return True  # unreadable → fail closed
        if ppid == server_pid:
            return True
        current = ppid
    # Reached PID 1 (init) without finding server → not a descendant
    return False


def signal_exact_matches(
    matches: tuple[ProcTokenMatch, ...],
    sig: int,
    token: str,
    owner_uid: int,
) -> SignalResult:
    """Signal matched processes using pidfd for PID-reuse safety.

    For every token match:
    1. Read PID/start ticks/UID
    2. Open os.pidfd_open(pid)
    3. Re-read stat and require same integer start ticks and UID
    4. Signal through signal.pidfd_send_signal
    5. Close the pidfd

    No os.kill, killpg, or ancestry heuristic.
    """
    signaled = 0
    failed = 0
    safety_aborts = 0

    for match in matches:
        pid = match.pid

        # Safety checks
        if pid in _PROTECTED_PIDS:
            safety_aborts += 1
            continue
        if _is_server_or_ancestor(pid):
            logger.error(
                "f138_safety_abort: token found on server/ancestor pid=%d token_hash=%s",
                pid,
                hash_token(token),
            )
            safety_aborts += 1
            continue

        try:
            # Open pidfd — binds the exact process
            pidfd = os.pidfd_open(pid)  # type: ignore[attr-defined]
        except (OSError, AttributeError) as e:
            logger.warning("f138_pidfd_open_failed pid=%d: %s", pid, e)
            failed += 1
            continue

        try:
            # Re-validate: same start_ticks and UID after pidfd opened
            current_ticks = _read_proc_start_ticks(pid)
            current_uid = _read_proc_uid(pid)
            if current_ticks != match.start_ticks or current_uid != owner_uid:
                logger.warning(
                    "f138_identity_changed pid=%d expected_ticks=%d got_ticks=%s "
                    "expected_uid=%d got_uid=%s",
                    pid,
                    match.start_ticks,
                    current_ticks,
                    owner_uid,
                    current_uid,
                )
                failed += 1
                continue

            # Signal through pidfd
            signal.pidfd_send_signal(pidfd, sig)  # type: ignore[attr-defined]
            signaled += 1
        except OSError as e:
            if e.errno == 3:  # ESRCH — already dead
                signaled += 1  # count as success
            else:
                logger.warning("f138_pidfd_signal_failed pid=%d sig=%d: %s", pid, sig, e)
                failed += 1
        finally:
            os.close(pidfd)

    return SignalResult(signaled=signaled, failed=failed, safety_aborts=safety_aborts)


# --- Fixed-point TERM then KILL -----------------------------------------------


def run_reconciliation_attempt_sync(
    token: str,
    owner_uid: int,
    token_hash: str,
    *,
    issuance_ticks: int | None = None,
    issuance_boot_id: str | None = None,
) -> ReconcileAttemptResult:
    """One reconciliation attempt: TERM → grace → KILL → fixed-point.

    This runs in a worker thread (called via asyncio.to_thread).
    """
    # D17: Boot-change short-circuit — if we rebooted since issuance, all
    # incarnation processes are definitionally gone (PIDs recycled).
    if issuance_boot_id is not None:
        current_boot = _read_boot_id()
        if current_boot is not None and current_boot != issuance_boot_id:
            return ReconcileAttemptResult(
                code="success",
                complete_scan=True,
                scanned=0,
                term_signaled=0,
                kill_signaled=0,
                residual=0,
                retry_delay_s=None,
                detail="boot_changed",
            )

    _scan_kw = {
        "issuance_ticks": issuance_ticks,
        "issuance_boot_id": issuance_boot_id,
    }

    # D4 barrier: No signal without a tombstone row. If no tombstone exists,
    # write one from what we can still see (writer='job'); if even that fails,
    # return tombstone_missing with a retry delay.
    from cli_agent_orchestrator.services.pane_tombstone_service import (
        require_tombstone,
        record_degenerate,
    )
    from cli_agent_orchestrator.clients.database import SessionLocal

    with SessionLocal() as db:
        tombstone_id = require_tombstone(token_hash, db)
        if tombstone_id is None:
            # T-2: Try to write a degenerate tombstone from the job side
            try:
                from cli_agent_orchestrator.services.session_degradation_service import (
                    resolve_session_incarnation,
                )
                from cli_agent_orchestrator.clients.database import (
                    ProcessIncarnationModel,
                    TerminalModel as _TerminalModel,
                )

                # N2: Resolve actual terminal_id from token_hash DB lookup
                inc_row = (
                    db.query(ProcessIncarnationModel).filter_by(token_hash=token_hash).one_or_none()
                )
                if inc_row is not None:
                    resolved_terminal_id = inc_row.terminal_id
                    resolved_generation = inc_row.terminal_generation
                else:
                    resolved_terminal_id = token_hash  # fallback: use hash as id
                    resolved_generation = 0

                # Resolve session_name from terminal if still in DB
                term_row = (
                    db.query(_TerminalModel.tmux_session)
                    .filter_by(id=resolved_terminal_id)
                    .one_or_none()
                )
                resolved_session = term_row[0] if term_row is not None else "orphan"

                result = record_degenerate(
                    db=db,
                    incarnation_id=token_hash,
                    terminal_id=resolved_terminal_id,
                    terminal_generation=resolved_generation,
                    session_name=resolved_session,
                    session_incarnation="degenerate",
                    scope="unknown",
                    writer="job",
                    incomplete_reason="evidence_age=post_restart",
                )
                db.commit()
                if result.error and not result.already_existed:
                    return ReconcileAttemptResult(
                        code="tombstone_missing",
                        complete_scan=False,
                        scanned=0,
                        term_signaled=0,
                        kill_signaled=0,
                        residual=0,
                        retry_delay_s=_RETRY_DELAYS[0],
                        detail="tombstone_write_failed",
                    )
            except Exception as e:
                logger.warning("f218_tombstone_barrier_failed: %s", e)
                return ReconcileAttemptResult(
                    code="tombstone_missing",
                    complete_scan=False,
                    scanned=0,
                    term_signaled=0,
                    kill_signaled=0,
                    residual=0,
                    retry_delay_s=_RETRY_DELAYS[0],
                    detail=f"tombstone_barrier_error: {type(e).__name__}",
                )

    # Step 1: complete token scan
    scan = scan_incarnation_processes(token, owner_uid, **_scan_kw)
    if not scan.complete:
        if not scan.matches:
            return ReconcileAttemptResult(
                code="scan_incomplete",
                complete_scan=False,
                scanned=len(scan.matches),
                term_signaled=0,
                kill_signaled=0,
                residual=len(scan.matches),
                retry_delay_s=_RETRY_DELAYS[0],
                detail="; ".join(scan.errors[:3]),
            )
        # D17: Has matches despite incomplete scan — signal them, but cannot
        # declare success (fall through to TERM/KILL logic; final success gate
        # still requires complete scan).

    if scan.complete and not scan.matches:
        # Need two consecutive complete empty scans for success
        time.sleep(_RESCAN_INTERVAL_S)
        scan2 = scan_incarnation_processes(token, owner_uid, **_scan_kw)
        if not scan2.complete:
            return ReconcileAttemptResult(
                code="scan_incomplete",
                complete_scan=False,
                scanned=0,
                term_signaled=0,
                kill_signaled=0,
                residual=0,
                retry_delay_s=_RETRY_DELAYS[0],
                detail="second_scan_incomplete",
            )
        if not scan2.matches:
            return ReconcileAttemptResult(
                code="success",
                complete_scan=True,
                scanned=0,
                term_signaled=0,
                kill_signaled=0,
                residual=0,
                retry_delay_s=None,
                detail="already_clean",
            )
        # Found matches on second scan — proceed with TERM
        scan = scan2

    total_scanned = len(scan.matches)

    # Step 2: pidfd-SIGTERM every safe match
    term_result = signal_exact_matches(scan.matches, signal.SIGTERM, token, owner_uid)
    term_signaled = term_result.signaled

    # Step 3: wait up to ORPHAN_TERM_GRACE_SECONDS, rescanning every 250ms
    grace_start = time.monotonic()
    empty_count = 0
    while (time.monotonic() - grace_start) < ORPHAN_TERM_GRACE_SECONDS:
        time.sleep(_RESCAN_INTERVAL_S)
        rescan = scan_incarnation_processes(token, owner_uid, **_scan_kw)
        if not rescan.complete:
            continue  # incomplete scan doesn't count
        if not rescan.matches:
            empty_count += 1
            # Step 4: two consecutive complete empty scans → success
            if empty_count >= _EMPTY_SCAN_CONFIRM_COUNT:
                return ReconcileAttemptResult(
                    code="success",
                    complete_scan=True,
                    scanned=total_scanned,
                    term_signaled=term_signaled,
                    kill_signaled=0,
                    residual=0,
                    retry_delay_s=None,
                    detail="term_sufficient",
                )
        else:
            empty_count = 0

    # Step 5: grace expired — rescan and SIGKILL remaining
    kill_scan = scan_incarnation_processes(token, owner_uid, **_scan_kw)
    kill_signaled = 0
    if kill_scan.matches:
        kill_result = signal_exact_matches(kill_scan.matches, signal.SIGKILL, token, owner_uid)
        kill_signaled = kill_result.signaled

    # Step 6: bounded post-KILL rounds to catch children forked between scans
    for _round in range(_POST_KILL_ROUNDS):
        time.sleep(_RESCAN_INTERVAL_S)
        post_scan = scan_incarnation_processes(token, owner_uid, **_scan_kw)
        if not post_scan.complete:
            continue
        if post_scan.matches:
            extra_kill = signal_exact_matches(post_scan.matches, signal.SIGKILL, token, owner_uid)
            kill_signaled += extra_kill.signaled
        else:
            # Check for second consecutive empty
            time.sleep(_RESCAN_INTERVAL_S)
            confirm_scan = scan_incarnation_processes(token, owner_uid, **_scan_kw)
            if confirm_scan.complete and not confirm_scan.matches:
                return ReconcileAttemptResult(
                    code="success",
                    complete_scan=True,
                    scanned=total_scanned,
                    term_signaled=term_signaled,
                    kill_signaled=kill_signaled,
                    residual=0,
                    retry_delay_s=None,
                    detail="kill_required",
                )

    # Step 7: final check
    final_scan = scan_incarnation_processes(token, owner_uid, **_scan_kw)
    residual = len(final_scan.matches) if final_scan.complete else -1
    if final_scan.complete and not final_scan.matches:
        return ReconcileAttemptResult(
            code="success",
            complete_scan=True,
            scanned=total_scanned,
            term_signaled=term_signaled,
            kill_signaled=kill_signaled,
            residual=0,
            retry_delay_s=None,
            detail="kill_required_final",
        )

    return ReconcileAttemptResult(
        code="residual" if final_scan.complete else "scan_incomplete",
        complete_scan=final_scan.complete,
        scanned=total_scanned,
        term_signaled=term_signaled,
        kill_signaled=kill_signaled,
        residual=max(0, residual),
        retry_delay_s=_RETRY_DELAYS[0],
        detail=f"residual_after_kill_rounds={residual}",
    )


# --- Yama/ptrace startup diagnostic ------------------------------------------


def check_yama_ptrace_scope() -> dict[str, Any]:
    """Read-only startup diagnostic for /proc/sys/kernel/yama/ptrace_scope.

    Returns structured evidence. Never weakens the exact-match law.
    """
    result: dict[str, Any] = {"checked": True, "value": None, "readable": False, "warning": None}
    yama_path = Path("/proc/sys/kernel/yama/ptrace_scope")
    try:
        value = int(yama_path.read_text().strip())
        result["readable"] = True
        result["value"] = value
        if value >= 2:
            result["warning"] = (
                f"yama/ptrace_scope={value} — process environ reading may be restricted. "
                "Likely causes: kernel ptrace/Yama policy, AppArmor, or SELinux. "
                "Orphan reconciliation scan_incomplete failures may result."
            )
    except (OSError, ValueError) as e:
        result["warning"] = (
            f"Cannot read yama/ptrace_scope ({e}). "
            "If orphan reconciliation reports scan_incomplete, check ptrace/Yama policy, "
            "AppArmor, or SELinux."
        )
    return result


# --- Liveness observation API -------------------------------------------------


def record_window_liveness_observation(
    *,
    terminal_id: str,
    terminal_generation: int,
    incarnation_id: str,
    state: Literal["live", "gone", "error"],
    source: str,
) -> LivenessObservationResult:
    """Record a typed liveness observation from an authoritative producer.

    `error` never queues cleanup. Two consecutive authoritative `gone`
    observations for the same terminal generation/incarnation queue one job.
    A `live` observation resets the confirmation counter.
    """
    from cli_agent_orchestrator.clients.database import (
        f138_record_liveness_observation,
    )

    return f138_record_liveness_observation(
        terminal_id=terminal_id,
        terminal_generation=terminal_generation,
        incarnation_id=incarnation_id,
        state=state,
        source=source,
    )


def request_orphan_reconciliation(incarnation_id: str, source: str) -> JobRequestResult:
    """Explicitly request reconciliation for an incarnation (e.g., on delete/rebind)."""
    from cli_agent_orchestrator.clients.database import f138_request_reconciliation

    return f138_request_reconciliation(incarnation_id=incarnation_id, source=source)


def record_confirmed_gone_observation(incarnation_id: str, source: str) -> "JobRequestResult":
    """D20/D24: Record a confirmed-gone observation — bypasses 2-submission threshold.

    Uses f138_force_reconcile_incarnation to handle ALL incarnation states
    (including abandoned/launching), not just active/reconcile_pending.
    Called from the FIFO watchdog's confirmed-gone path after D15 two-tick or
    cold-start/rearm exhaustion, using the pinned enrollment authority (D19).
    """
    from cli_agent_orchestrator.clients.database import f138_force_reconcile_incarnation

    fr = f138_force_reconcile_incarnation(incarnation_id, source=source)
    # Adapt ForceReconcileResult to JobRequestResult for backward compat
    created = fr.outcome == "created"
    return JobRequestResult(created=created, job_id=fr.job_id, detail=fr.detail)


def _f166_notify_once(
    *,
    job_id: str,
    failure_code: str,
    message_builder: "callable",
) -> bool:
    """F166 D7: Key-agnostic notify-once helper with (job_id, failure_code) dedup + cap.

    Returns True if a notification was emitted, False if suppressed.
    Dedup: skips when notified_failure_code already equals failure_code.
    Cap: notify_count >= MAX_NOTIFICATIONS_PER_JOB suppresses with a WARN log.
    Counter increments ONLY on a successful create_inbox_message.
    """
    from cli_agent_orchestrator.clients.database import (
        OrphanReconcileJobModel,
        SessionLocal,
    )
    from cli_agent_orchestrator.services.mailbox_service import (
        get_current_supervisor_terminal_id,
    )

    supervisor_id = get_current_supervisor_terminal_id()
    if supervisor_id is None:
        logger.warning("f166_notify_no_supervisor: job=%s failure=%s", job_id, failure_code)
        return False

    with SessionLocal() as db:
        job = db.query(OrphanReconcileJobModel).filter_by(id=job_id).one_or_none()
        if job is None:
            return False
        current_code = job.notified_failure_code
        current_count = job.notify_count or 0

    if current_code == failure_code:
        return False

    if current_count >= MAX_NOTIFICATIONS_PER_JOB:
        logger.warning(
            "f166_notify_cap_exceeded: job=%s count=%d cap=%d failure=%s",
            job_id,
            current_count,
            MAX_NOTIFICATIONS_PER_JOB,
            failure_code,
        )
        return False

    message = message_builder(supervisor_id)
    try:
        from cli_agent_orchestrator.clients.database import create_inbox_message

        create_inbox_message(
            sender_id="system",
            receiver_id=supervisor_id,
            message=message,
        )
    except Exception:
        logger.exception("f166_notify_send_failed: job=%s failure=%s", job_id, failure_code)
        return False

    with SessionLocal.begin() as db:
        job = db.query(OrphanReconcileJobModel).filter_by(id=job_id).one_or_none()
        if job is not None:
            job.notified_failure_code = failure_code
            job.notify_count = (job.notify_count or 0) + 1

    return True


def f138_notify_confirmed_gone_report_failed(
    *,
    job_id: str,
    terminal_id: str,
    terminal_generation: int | None,
    source: str,
    detail: str,
    safe_reference: str,
) -> None:
    """D20: Thread-safe DB-only attention notification for FIFO watchdog.

    Callable from the watchdog thread (no async, no lock held across slow work).
    Payload carries terminal ID/generation/source/detail and a safe hash/reference,
    never the raw token. Routes through F166 _f166_notify_once for dedup + cap.
    """
    failure_code = f"confirmed_gone_report_failed:{detail}"

    def build_message(supervisor_id: str) -> str:
        return (
            f"[F138] confirmed_gone_report_failed: terminal={terminal_id} "
            f"gen={terminal_generation} source={source} detail={detail} "
            f"ref={safe_reference}. Enrollment retained; bounded retries continue. "
            f"Manual investigation may be needed."
        )

    _f166_notify_once(
        job_id=job_id,
        failure_code=failure_code,
        message_builder=build_message,
    )


# --- OrphanReconcileService (dispatcher) --------------------------------------


class OrphanReconcileService:
    """Bounded background dispatcher for orphan reconciliation jobs.

    - One dirty event/epoch, not one task per observation.
    - Maximum two concurrent jobs.
    - Batch claim cap of ten.
    - 30-second lease, renewed around long waits.
    - Capped retry delays.
    - Periodic 30-second fallback wake for missed in-memory signals.
    """

    def __init__(self) -> None:
        self._dirty = asyncio.Event()
        self._stop = False
        self._task: asyncio.Task[None] | None = None

    def signal_dirty(self) -> None:
        """Signal that new work may be available."""
        self._dirty.set()

    async def startup_recovery(self) -> None:
        """At server startup: expire stale leases and schedule due work."""
        from cli_agent_orchestrator.clients.database import f138_startup_recovery

        f138_startup_recovery()
        # Check Yama diagnostic
        diag = check_yama_ptrace_scope()
        if diag.get("warning"):
            logger.warning("f138_yama_diagnostic: %s", diag["warning"])
        self.signal_dirty()

    async def run(self) -> None:
        """Main dispatcher loop."""
        try:
            while not self._stop:
                self._dirty.clear()
                await self._dispatch_batch()
                # Wait for either a signal or the periodic fallback
                try:
                    await asyncio.wait_for(self._dirty.wait(), timeout=_DISPATCHER_POLL_S)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _dispatch_batch(self) -> None:
        """Claim and execute up to _MAX_CONCURRENT_JOBS jobs."""
        from cli_agent_orchestrator.clients.database import (
            f138_claim_jobs,
            f138_complete_job,
            f138_get_incarnation_for_job,
            f138_renew_lease,
            f138_retry_job,
        )

        jobs = f138_claim_jobs(limit=_BATCH_CLAIM_CAP, lease_duration_s=_LEASE_DURATION_S)
        if not jobs:
            return

        # Run up to MAX_CONCURRENT_JOBS in parallel
        sem = asyncio.Semaphore(_MAX_CONCURRENT_JOBS)

        async def _run_one(job_id: str, incarnation_id: str) -> None:
            async with sem:
                await self._execute_job(job_id, incarnation_id)

        tasks = [
            asyncio.create_task(_run_one(job["id"], job["incarnation_id"]))
            for job in jobs[:_BATCH_CLAIM_CAP]
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute_job(self, job_id: str, incarnation_id: str) -> None:
        """Execute a single reconciliation job."""
        from cli_agent_orchestrator.clients.database import (
            f138_complete_job,
            f138_get_incarnation_for_job,
            f138_get_job_attempt,
            f138_mark_attention_required,
            f138_renew_lease,
            f138_retry_job,
        )

        incarnation = f138_get_incarnation_for_job(incarnation_id)
        if incarnation is None:
            logger.error("f138_job_orphan: job=%s incarnation=%s not found", job_id, incarnation_id)
            f138_complete_job(job_id, "succeeded", detail="incarnation_missing")
            return

        attempt = f138_get_job_attempt(job_id)
        token_hash_val = incarnation["token_hash"]

        logger.info(
            "f138_reconcile_start job=%s terminal=%s/%d token_hash=%s attempt=%d",
            job_id,
            incarnation["terminal_id"],
            incarnation["terminal_generation"],
            token_hash_val,
            attempt,
        )

        start_mono = time.monotonic()
        try:
            result = await asyncio.to_thread(
                run_reconciliation_attempt_sync,
                incarnation["token"],
                incarnation["owner_uid"],
                token_hash_val,
                issuance_ticks=incarnation.get("issuance_ticks"),
                issuance_boot_id=incarnation.get("issuance_boot_id"),
            )
        except Exception as e:
            logger.exception("f138_reconcile_error job=%s: %s", job_id, e)
            delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
            f138_retry_job(job_id, delay)
            return

        elapsed = time.monotonic() - start_mono

        logger.info(
            "f138_reconcile_result job=%s token_hash=%s code=%s "
            "scanned=%d term=%d kill=%d residual=%d complete=%s elapsed=%.2fs",
            job_id,
            token_hash_val,
            result.code,
            result.scanned,
            result.term_signaled,
            result.kill_signaled,
            result.residual,
            result.complete_scan,
            elapsed,
        )

        if result.code == "success":
            f138_complete_job(job_id, "succeeded", detail=result.detail)
            # Mark incarnation reconciled
            from cli_agent_orchestrator.clients.database import f138_mark_incarnation_reconciled

            f138_mark_incarnation_reconciled(incarnation_id)
        else:
            # Determine if attention_required
            delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
            # F166-F1: fast-track permanently-unprovable scans — don't waste
            # all 8 retry delays on a condition that cannot resolve.
            is_permanent = _is_permanent_failure(result.detail)
            if attempt >= len(_RETRY_DELAYS) - 1 or is_permanent:
                # Max retries exhausted OR known-permanent failure
                f138_mark_attention_required(job_id, result.code)
                await self._notify_attention_required(
                    job_id,
                    incarnation["terminal_id"],
                    token_hash_val,
                    result.code,
                    result.detail,
                )
                if is_permanent:
                    logger.info(
                        "f166_permanent_fast_track job=%s code=%s detail=%s attempt=%d",
                        job_id,
                        result.code,
                        result.detail,
                        attempt,
                    )
            else:
                f138_retry_job(job_id, delay)

    async def _notify_attention_required(
        self,
        job_id: str,
        terminal_id: str,
        token_hash_val: str,
        failure_code: str,
        detail: str | None,
    ) -> None:
        """Notify current live supervisor about attention-required failure.

        Routes through _f166_notify_once for (job_id, failure_code) dedup + cap.
        """

        def build_message(supervisor_id: str) -> str:
            return (
                f"[F138] Orphan reconciliation attention required for terminal {terminal_id}. "
                f"Failure: {failure_code}. Detail: {detail or 'none'}. "
                f"Token hash: {token_hash_val}. Manual intervention may be needed."
            )

        _f166_notify_once(
            job_id=job_id,
            failure_code=failure_code,
            message_builder=build_message,
        )

    def stop(self) -> None:
        self._stop = True
        self._dirty.set()


# Module-level singleton
orphan_reconcile_service = OrphanReconcileService()
