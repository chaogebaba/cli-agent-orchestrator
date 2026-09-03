"""F218-a: Pane exit tombstone service — forensic record of confirmed-gone panes.

The tombstone is written BEFORE any signal (TERM/KILL) and carries the last
evidence readable from /proc and cgroup while the process tree still exists.
Collectors never raise — every failure becomes a *_status + *_reason pair (D10).
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ─── Evidence dataclasses (never raise) ──────────────────────────────────────


@dataclass(frozen=True)
class ProcessEvidence:
    """Collected process-identity evidence. All fields nullable."""

    pane_pid: int | None = None
    pane_start_ticks: int | None = None
    pane_pgid: int | None = None
    issuance_boot_id: str | None = None
    matched_pids_json: str | None = None
    cgroup_path: str | None = None
    systemd_scope: str | None = None
    status: str = "unavailable"  # ok|unavailable|denied|not_applicable
    reason: str | None = None


@dataclass(frozen=True)
class MemoryEvidence:
    """Collected memory/pressure evidence. All fields nullable."""

    memory_events_json: str | None = None
    memory_current: int | None = None
    memory_peak: int | None = None
    memory_max: str | None = None  # N1: RAW cgroup file content, verbatim — no coercion
    memory_pressure_json: str | None = None
    status: str = "unavailable"  # ok|unavailable|denied|not_applicable
    reason: str | None = None


@dataclass(frozen=True)
class TombstoneResult:
    """Result of a tombstone write attempt."""

    tombstone_id: str | None = None
    created: bool = False
    already_existed: bool = False
    incomplete: bool = False
    error: str | None = None


# ─── Collectors (never raise) ────────────────────────────────────────────────


def collect_process_evidence(pane_pid: int | None) -> ProcessEvidence:
    """Collect process identity from /proc. NEVER raises — failures → status/reason."""
    if pane_pid is None:
        return ProcessEvidence(status="not_applicable", reason="no_pane_pid")

    try:
        proc_dir = Path(f"/proc/{pane_pid}")
        if not proc_dir.exists():
            return ProcessEvidence(
                pane_pid=pane_pid,
                status="unavailable",
                reason="process_already_reaped",
            )

        # Read start time (ticks) from /proc/<pid>/stat
        start_ticks: int | None = None
        try:
            stat_content = (proc_dir / "stat").read_text()
            # Field 22 (0-indexed) is starttime
            parts = stat_content.split(")")[-1].split()
            if len(parts) >= 20:
                start_ticks = int(parts[19])  # starttime is field 22, index 19 after ')'
        except (OSError, ValueError, IndexError):
            pass

        # Process group ID
        pgid: int | None = None
        try:
            pgid = os.getpgid(pane_pid)
        except (OSError, ProcessLookupError):
            pass

        # Boot ID
        boot_id: str | None = None
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            pass

        # Cgroup path
        cgroup_path: str | None = None
        systemd_scope: str | None = None
        try:
            cgroup_content = (proc_dir / "cgroup").read_text()
            for line in cgroup_content.splitlines():
                parts = line.split(":", 2)
                if len(parts) == 3:
                    cgroup_path = parts[2]
                    # Extract systemd scope from cgroup path
                    if ".scope" in cgroup_path:
                        scope_part = cgroup_path.rsplit("/", 1)[-1]
                        if scope_part.endswith(".scope"):
                            systemd_scope = scope_part
                    break
        except OSError:
            pass

        # Scan for matched pids (children of pane_pid)
        matched_pids: list[int] = []
        try:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    status = (entry / "status").read_text()
                    for sline in status.splitlines():
                        if sline.startswith("PPid:"):
                            ppid = int(sline.split(":")[1].strip())
                            if ppid == pane_pid:
                                matched_pids.append(int(entry.name))
                            break
                except (OSError, ValueError):
                    continue
        except OSError:
            pass

        return ProcessEvidence(
            pane_pid=pane_pid,
            pane_start_ticks=start_ticks,
            pane_pgid=pgid,
            issuance_boot_id=boot_id,
            matched_pids_json=json.dumps(matched_pids) if matched_pids else None,
            cgroup_path=cgroup_path,
            systemd_scope=systemd_scope,
            status="ok",
            reason=None,
        )
    except Exception as e:
        return ProcessEvidence(
            pane_pid=pane_pid,
            status="unavailable",
            reason=f"collection_error: {type(e).__name__}",
        )


def collect_memory_evidence(cgroup_path: str | None) -> MemoryEvidence:
    """Collect cgroup memory evidence. NEVER raises — failures → status/reason."""
    if cgroup_path is None:
        return MemoryEvidence(status="not_applicable", reason="no_cgroup_path")

    try:
        # Resolve the cgroup v2 filesystem path
        cgroup_base = Path("/sys/fs/cgroup") / cgroup_path.lstrip("/")
        if not cgroup_base.exists():
            return MemoryEvidence(status="unavailable", reason="cgroup_path_not_found")

        memory_events_json: str | None = None
        memory_current: int | None = None
        memory_peak: int | None = None
        memory_max: str | None = None  # N1: stored verbatim
        memory_pressure_json: str | None = None

        # memory.events
        try:
            events_path = cgroup_base / "memory.events"
            if events_path.exists():
                content = events_path.read_text().strip()
                events_dict = {}
                for line in content.splitlines():
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        events_dict[parts[0]] = int(parts[1])
                memory_events_json = json.dumps(events_dict)
        except (OSError, ValueError):
            pass

        # memory.current
        try:
            current_path = cgroup_base / "memory.current"
            if current_path.exists():
                memory_current = int(current_path.read_text().strip())
        except (OSError, ValueError):
            pass

        # memory.peak (may not exist on all kernels)
        try:
            peak_path = cgroup_base / "memory.peak"
            if peak_path.exists():
                memory_peak = int(peak_path.read_text().strip())
        except (OSError, ValueError):
            pass

        # memory.max — N1: RAW content, verbatim. No coercion.
        try:
            max_path = cgroup_base / "memory.max"
            if max_path.exists():
                raw = max_path.read_text().strip()
                memory_max = raw  # Store EXACTLY as read — "max" or decimal string
        except OSError:
            pass

        # memory.pressure
        try:
            pressure_path = cgroup_base / "memory.pressure"
            if pressure_path.exists():
                memory_pressure_json = json.dumps(
                    {"memory_pressure": pressure_path.read_text().strip()}
                )
        except OSError:
            pass

        has_any = any(
            v is not None for v in (memory_events_json, memory_current, memory_peak, memory_max)
        )
        return MemoryEvidence(
            memory_events_json=memory_events_json,
            memory_current=memory_current,
            memory_peak=memory_peak,
            memory_max=memory_max,
            memory_pressure_json=memory_pressure_json,
            status="ok" if has_any else "unavailable",
            reason=None if has_any else "no_readable_memory_files",
        )
    except Exception as e:
        return MemoryEvidence(
            status="unavailable",
            reason=f"collection_error: {type(e).__name__}",
        )


# ─── Service functions ────────────────────────────────────────────────────────


def record(
    *,
    db: Session,
    incarnation_id: str,
    terminal_id: str,
    terminal_generation: int,
    token_hash: str | None,
    session_name: str,
    session_incarnation: str,
    scope_probe: "ScopeProbe",
    scope_hint: str | None,
    writer: Literal["observation", "job"],
    window_name: str | None = None,
    window_index: int | None = None,
    pane_id: str | None = None,
    pane_pid: int | None = None,
    forensics_enabled: bool = True,
) -> TombstoneResult:
    """Write a tombstone. D3/D4: BEFORE any signal. INSERT … ON CONFLICT DO NOTHING."""
    from cli_agent_orchestrator.backends.base import ScopeProbe
    from cli_agent_orchestrator.clients.database import PaneExitTombstoneModel

    now = datetime.now(timezone.utc)
    tombstone_id = uuid.uuid4().hex[:16]

    # Collect forensics if enabled
    if forensics_enabled and pane_pid is not None:
        proc_ev = collect_process_evidence(pane_pid)
        mem_ev = collect_memory_evidence(proc_ev.cgroup_path)
    else:
        proc_ev = ProcessEvidence(
            status="not_applicable" if not forensics_enabled else "unavailable",
            reason="forensics_disabled" if not forensics_enabled else "no_pane_pid",
        )
        mem_ev = MemoryEvidence(
            status="not_applicable" if not forensics_enabled else "unavailable",
            reason="forensics_disabled" if not forensics_enabled else "no_evidence_path",
        )

    complete = (proc_ev.status == "ok" or proc_ev.status == "not_applicable") and (
        mem_ev.status == "ok" or mem_ev.status == "not_applicable"
    )

    sibling_json: str | None = None
    if scope_probe.sibling_windows is not None:
        sibling_json = json.dumps(list(scope_probe.sibling_windows))

    row = PaneExitTombstoneModel(
        id=tombstone_id,
        incarnation_id=incarnation_id,
        terminal_id=terminal_id,
        terminal_generation=terminal_generation,
        token_hash=token_hash,
        session_name=session_name,
        session_incarnation=session_incarnation,
        scope=scope_probe.scope,
        scope_hint=scope_hint,
        scope_evidence_json=json.dumps(list(scope_probe.evidence)),
        confirm_samples=scope_probe.samples,
        window_name=window_name,
        window_index=window_index,
        pane_id=pane_id,
        sibling_windows_json=sibling_json,
        pane_pid=proc_ev.pane_pid,
        pane_start_ticks=proc_ev.pane_start_ticks,
        pane_pgid=proc_ev.pane_pgid,
        issuance_boot_id=proc_ev.issuance_boot_id,
        matched_pids_json=proc_ev.matched_pids_json,
        cgroup_path=proc_ev.cgroup_path,
        systemd_scope=proc_ev.systemd_scope,
        proc_status=proc_ev.status,
        proc_reason=proc_ev.reason,
        exit_code=None,
        term_signal=None,
        exit_evidence_status="unavailable_no_waiter",
        exit_evidence_reason="tmux reaps pane child; CAO is not the waiter",
        memory_events_json=mem_ev.memory_events_json,
        memory_current=mem_ev.memory_current,
        memory_peak=mem_ev.memory_peak,
        memory_max=mem_ev.memory_max,
        memory_pressure_json=mem_ev.memory_pressure_json,
        memory_status=mem_ev.status,
        memory_reason=mem_ev.reason,
        writer=writer,
        schema_version=1,
        complete=complete,
        incomplete_reason=None if complete else f"proc={proc_ev.status};mem={mem_ev.status}",
        observed_at=now,
        written_at=now,
        server_pid=os.getpid(),
        server_boot_id=proc_ev.issuance_boot_id,
    )

    from sqlalchemy.exc import IntegrityError

    try:
        db.add(row)
        db.flush()
        logger.info(
            "f218_tombstone id=%s terminal=%s/%s incarnation=%s scope=%s writer=%s "
            "complete=%s proc=%s memory=%s exit_evidence=%s matched_pids=%s",
            tombstone_id,
            terminal_id,
            terminal_generation,
            incarnation_id[:8],
            scope_probe.scope,
            writer,
            complete,
            proc_ev.status,
            mem_ev.status,
            "unavailable_no_waiter",
            len(json.loads(proc_ev.matched_pids_json)) if proc_ev.matched_pids_json else 0,
        )
        return TombstoneResult(tombstone_id=tombstone_id, created=True)
    except IntegrityError:
        db.rollback()
        # Already exists for this incarnation — idempotent
        return TombstoneResult(tombstone_id=None, already_existed=True)
    except Exception as e:
        db.rollback()
        logger.warning("f218_tombstone_write_failed: %s: %s", type(e).__name__, e)
        return TombstoneResult(error=f"{type(e).__name__}: {e}")


def record_degenerate(
    *,
    db: Session,
    incarnation_id: str,
    terminal_id: str,
    terminal_generation: int,
    session_name: str,
    session_incarnation: str,
    scope: str,
    writer: Literal["observation", "job"],
    incomplete_reason: str,
) -> TombstoneResult:
    """D11: Write a degenerate tombstone when full collection fails. Never blocks signal."""
    from cli_agent_orchestrator.clients.database import PaneExitTombstoneModel
    from sqlalchemy.exc import IntegrityError

    now = datetime.now(timezone.utc)
    tombstone_id = uuid.uuid4().hex[:16]

    row = PaneExitTombstoneModel(
        id=tombstone_id,
        incarnation_id=incarnation_id,
        terminal_id=terminal_id,
        terminal_generation=terminal_generation,
        token_hash=None,
        session_name=session_name,
        session_incarnation=session_incarnation,
        scope=scope,
        scope_hint=None,
        scope_evidence_json=None,
        confirm_samples=None,
        window_name=None,
        window_index=None,
        pane_id=None,
        sibling_windows_json=None,
        pane_pid=None,
        pane_start_ticks=None,
        pane_pgid=None,
        issuance_boot_id=None,
        matched_pids_json=None,
        cgroup_path=None,
        systemd_scope=None,
        proc_status="unavailable",
        proc_reason=incomplete_reason,
        exit_code=None,
        term_signal=None,
        exit_evidence_status="unavailable_no_waiter",
        exit_evidence_reason=incomplete_reason,
        memory_events_json=None,
        memory_current=None,
        memory_peak=None,
        memory_max=None,
        memory_pressure_json=None,
        memory_status="unavailable",
        memory_reason=incomplete_reason,
        writer=writer,
        schema_version=1,
        complete=False,
        incomplete_reason=incomplete_reason,
        observed_at=now,
        written_at=now,
        server_pid=os.getpid(),
        server_boot_id=None,
    )

    try:
        db.add(row)
        db.flush()
        logger.info(
            "f218_tombstone id=%s terminal=%s/%s incarnation=%s scope=%s writer=%s "
            "complete=False proc=unavailable memory=unavailable exit_evidence=unavailable_no_waiter "
            "matched_pids=0",
            tombstone_id,
            terminal_id,
            terminal_generation,
            incarnation_id[:8],
            scope,
            writer,
        )
        return TombstoneResult(tombstone_id=tombstone_id, created=True, incomplete=True)
    except IntegrityError:
        db.rollback()
        return TombstoneResult(tombstone_id=None, already_existed=True)
    except Exception as e:
        db.rollback()
        return TombstoneResult(error=f"{type(e).__name__}: {e}")


def require_tombstone(incarnation_id: str, db: Session) -> str | None:
    """D4 barrier: return tombstone_id if a tombstone exists, else None (abort signal)."""
    from cli_agent_orchestrator.clients.database import PaneExitTombstoneModel

    row = (
        db.query(PaneExitTombstoneModel.id)
        .filter(PaneExitTombstoneModel.incarnation_id == incarnation_id)
        .first()
    )
    return row[0] if row else None


def upsert_fill(incarnation_id: str, db: Session, **fields: object) -> TombstoneResult:
    """T-2: UPDATE … WHERE col IS NULL per field. T-1's fresher data never overwritten."""
    from cli_agent_orchestrator.clients.database import PaneExitTombstoneModel

    row = (
        db.query(PaneExitTombstoneModel)
        .filter(PaneExitTombstoneModel.incarnation_id == incarnation_id)
        .first()
    )
    if row is None:
        return TombstoneResult(error="tombstone_not_found")

    updated = False
    for col_name, value in fields.items():
        if hasattr(row, col_name) and getattr(row, col_name) is None and value is not None:
            setattr(row, col_name, value)
            updated = True

    if updated:
        db.flush()
    return TombstoneResult(tombstone_id=row.id, created=False)
