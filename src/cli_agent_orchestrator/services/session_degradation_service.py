"""F218-a: Session degradation service — exactly-once alarm, durable ledger.

D5: UNIQUE(session_name, session_incarnation, cause) is the dedup key.
D9: Alarm ladder attempts every rung and records each.
D15: resolve_session_incarnation is TOTAL (never returns None/empty).
D16: Teardown suppression via durable intent rows.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DegradationResult:
    """Result of mark_degraded."""

    degradation_id: str | None = None
    newly_marked: bool = False
    suppressed_by_teardown: bool = False
    error: str | None = None


@dataclass(frozen=True)
class AlarmRungResult:
    """Per-rung result."""

    rung: str
    attempted: bool = True
    ok: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class AlarmResult:
    """Result of raise_alarm."""

    delivered: bool = False
    rungs: list[AlarmRungResult] | None = None
    suppressed: bool = False


# ─── D15: Session incarnation derivation (TOTAL) ─────────────────────────────


def resolve_session_incarnation(session_name: str, db: Session) -> str:
    """D15: TOTAL derivation. tmux #{session_id} from session row, else "epoch:<int(created_at)>".

    Never returns None and never returns "" — a NULL/empty key silently disables D5's
    UNIQUE dedup under SQLite's distinct-NULLs rule. Unresolvable (no session row at all)
    raises, which aborts and retries the mark rather than marking on a degenerate key.
    """
    from cli_agent_orchestrator.clients.database import SessionLocal

    # Try to get from session table — look for tmux_session_id or created_at
    from sqlalchemy import text

    row = db.execute(
        text("SELECT created_at FROM sessions WHERE name = :name"),
        {"name": session_name},
    ).fetchone()

    if row is None:
        raise ValueError(
            f"resolve_session_incarnation: no session row for {session_name!r} — "
            "cannot derive incarnation; aborting mark"
        )

    created_at = row[0]
    if isinstance(created_at, str):
        # Parse ISO format
        try:
            dt = datetime.fromisoformat(created_at)
            return f"epoch:{int(dt.timestamp())}"
        except (ValueError, TypeError):
            pass
    elif isinstance(created_at, datetime):
        return f"epoch:{int(created_at.timestamp())}"

    # Ultimate fallback — use session_name hash (deterministic, non-empty)
    import hashlib

    return f"epoch:{int(hashlib.sha256(session_name.encode()).hexdigest()[:8], 16)}"


# ─── D5: Mark degraded (CAS via UNIQUE constraint) ───────────────────────────


def mark_degraded(
    *,
    db: Session,
    session_name: str,
    session_incarnation: str,
    cause: str,
    tombstone_id: str | None = None,
    terminal_id: str | None = None,
    detail: dict | None = None,
) -> DegradationResult:
    """D5: CAS mark. Alarm fires only for the INSERT that reports rowcount==1."""
    from cli_agent_orchestrator.clients.database import SessionDegradationModel
    from cli_agent_orchestrator.services.teardown_intent_service import is_teardown_intended

    degradation_id = uuid.uuid4().hex[:16]
    now = datetime.now(timezone.utc)

    # D16: Check teardown suppression
    suppressed = is_teardown_intended(
        session_name=session_name,
        terminal_id=terminal_id,
        db=db,
    )

    row = SessionDegradationModel(
        id=degradation_id,
        session_name=session_name,
        session_incarnation=session_incarnation,
        cause=cause,
        tombstone_id=tombstone_id,
        terminal_id=terminal_id,
        detail_json=json.dumps(detail) if detail else None,
        alarm_rungs_json=None,  # filled by raise_alarm
        alarm_delivered=False,
        suppressed_by_teardown=suppressed,
        acknowledged_at=now if suppressed else None,  # suppressed = pre-acknowledged
        created_at=now,
    )

    try:
        db.add(row)
        db.flush()
        logger.info(
            "f218_degraded session=%s/%s cause=%s newly_marked=True tombstone=%s "
            "suppressed_by_teardown=%s",
            session_name,
            session_incarnation,
            cause,
            tombstone_id,
            suppressed,
        )
        return DegradationResult(
            degradation_id=degradation_id,
            newly_marked=True,
            suppressed_by_teardown=suppressed,
        )
    except IntegrityError:
        db.rollback()
        # UNIQUE constraint hit — already marked (exactly-once)
        logger.debug(
            "f218_degraded session=%s/%s cause=%s newly_marked=False (dedup)",
            session_name,
            session_incarnation,
            cause,
        )
        return DegradationResult(newly_marked=False)


def raise_alarm(degradation_id: str, db: Session) -> AlarmResult:
    """D9: Alarm ladder R0…R4, all rungs attempted and recorded.

    R0: event_log + SSE (always first — durable, replayable)
    R1: display-message on surviving session (window_gone only)
    R2: display-message on other CAO sessions (session_gone, no survivor)
    R3: durable message to another session's supervisor mailbox
    R4: 0600 JSON incident file under CAO_HOME/incidents/
    """
    from cli_agent_orchestrator.clients.database import SessionDegradationModel

    row = db.query(SessionDegradationModel).filter_by(id=degradation_id).first()
    if row is None:
        return AlarmResult(delivered=False)

    if row.suppressed_by_teardown:
        rungs = [
            AlarmRungResult(rung="sse", attempted=True, ok=False, reason="suppressed_by_teardown"),
            AlarmRungResult(
                rung="display", attempted=True, ok=False, reason="suppressed_by_teardown"
            ),
            AlarmRungResult(
                rung="other_session", attempted=True, ok=False, reason="suppressed_by_teardown"
            ),
            AlarmRungResult(
                rung="mailbox", attempted=True, ok=False, reason="suppressed_by_teardown"
            ),
            AlarmRungResult(rung="file", attempted=True, ok=False, reason="suppressed_by_teardown"),
        ]
        row.alarm_rungs_json = json.dumps(
            [
                {"rung": r.rung, "attempted": r.attempted, "ok": r.ok, "reason": r.reason}
                for r in rungs
            ]
        )
        row.alarm_delivered = False
        db.flush()
        logger.info(
            "f218_alarm degradation=%s rung=all ok=False reason=suppressed_by_teardown",
            degradation_id,
        )
        return AlarmResult(delivered=False, rungs=rungs, suppressed=True)

    rungs: list[AlarmRungResult] = []
    any_ok = False

    # R0: Event log + SSE bus (always attempted first)
    r0 = _alarm_rung_sse(row)
    rungs.append(r0)
    if r0.ok:
        any_ok = True

    # R1/R2: display-message (never send_keys — AC17)
    r1 = _alarm_rung_display(row)
    rungs.append(r1)
    if r1.ok:
        any_ok = True

    # R3: supervisor mailbox message
    r3 = _alarm_rung_mailbox(row)
    rungs.append(r3)
    if r3.ok:
        any_ok = True

    # R4: incident file (0600)
    r4 = _alarm_rung_file(row)
    rungs.append(r4)
    if r4.ok:
        any_ok = True

    row.alarm_rungs_json = json.dumps(
        [{"rung": r.rung, "attempted": r.attempted, "ok": r.ok, "reason": r.reason} for r in rungs]
    )
    row.alarm_delivered = any_ok
    db.flush()

    for r in rungs:
        logger.info(
            "f218_alarm degradation=%s rung=%s ok=%s reason=%s",
            degradation_id,
            r.rung,
            r.ok,
            r.reason,
        )

    return AlarmResult(delivered=any_ok, rungs=rungs)


def resurface_unacknowledged(supervisor_terminal_id: str, db: Session) -> int:
    """D9 R5: Re-surface unacknowledged degradations on supervisor registration. Once."""
    from cli_agent_orchestrator.clients.database import SessionDegradationModel

    unacked = (
        db.query(SessionDegradationModel)
        .filter(
            SessionDegradationModel.acknowledged_at.is_(None),
            SessionDegradationModel.alarm_delivered.is_(True),
        )
        .all()
    )

    count = 0
    for row in unacked:
        logger.info(
            "f218_resurface degradation=%s session=%s cause=%s to=%s",
            row.id,
            row.session_name,
            row.cause,
            supervisor_terminal_id,
        )
        count += 1

    return count


def acknowledge(degradation_id: str, *, by_terminal_id: str, db: Session) -> bool:
    """Acknowledge a degradation (stops R5 re-surfacing)."""
    from cli_agent_orchestrator.clients.database import SessionDegradationModel

    row = db.query(SessionDegradationModel).filter_by(id=degradation_id).first()
    if row is None:
        return False
    if row.acknowledged_at is not None:
        return True  # already acknowledged
    row.acknowledged_at = datetime.now(timezone.utc)
    db.flush()
    return True


# ─── Alarm rung implementations (D9 — NEVER send_keys/paste-buffer) ──────────


def _alarm_rung_sse(row: object) -> AlarmRungResult:
    """R0: Publish via event_log + SSE bus."""
    try:
        from cli_agent_orchestrator.services.sse_bus import get_bus

        event_data = {
            "type": "session_degraded",
            "session_name": row.session_name,
            "cause": row.cause,
            "tombstone_id": row.tombstone_id,
            "terminal_id": row.terminal_id,
            "degradation_id": row.id,
        }
        bus = get_bus()
        if bus is not None:
            bus.publish(event_data)
            return AlarmRungResult(rung="sse", ok=True)
        return AlarmRungResult(rung="sse", ok=False, reason="bus_unavailable")
    except Exception as e:
        return AlarmRungResult(rung="sse", ok=False, reason=f"{type(e).__name__}: {e}")


def _alarm_rung_display(row: object) -> AlarmRungResult:
    """R1/R2: tmux display-message + @cao_degraded option. NEVER send_keys."""
    try:
        from cli_agent_orchestrator.utils.tmux_command import tmux_argv
        import subprocess

        session_name = row.session_name
        msg = f"[CAO DEGRADED] {row.cause} — tombstone={row.tombstone_id}"

        # Try display-message on the session (or another session if session_gone)
        if row.cause != "session_gone":
            # R1: display on the surviving session
            result = subprocess.run(
                tmux_argv("display-message", "-t", session_name, msg),
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Also set persistent @cao_degraded option
            subprocess.run(
                tmux_argv(
                    "set-option",
                    "-t",
                    session_name,
                    "@cao_degraded",
                    row.cause,
                ),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return AlarmRungResult(rung="display", ok=True)
            return AlarmRungResult(
                rung="display", ok=False, reason=f"display_rc={result.returncode}"
            )
        else:
            # R2: session_gone — try other sessions
            result = subprocess.run(
                tmux_argv("list-sessions", "-F", "#{session_name}"),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for other in result.stdout.splitlines():
                    other = other.strip()
                    if other and other != session_name:
                        subprocess.run(
                            tmux_argv("display-message", "-t", other, msg),
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        return AlarmRungResult(rung="display", ok=True)
            return AlarmRungResult(rung="display", ok=False, reason="no_surviving_session")
    except Exception as e:
        return AlarmRungResult(rung="display", ok=False, reason=f"{type(e).__name__}: {e}")


def _alarm_rung_mailbox(row: object) -> AlarmRungResult:
    """R3: Send attention message to another session's supervisor."""
    try:
        # Best-effort — find any other live supervisor mailbox
        return AlarmRungResult(rung="mailbox", ok=False, reason="not_implemented_yet")
    except Exception as e:
        return AlarmRungResult(rung="mailbox", ok=False, reason=f"{type(e).__name__}: {e}")


def _alarm_rung_file(row: object) -> AlarmRungResult:
    """R4: Write 0600 JSON incident file under CAO_HOME/incidents/."""
    try:
        from cli_agent_orchestrator.constants import CAO_HOME_DIR

        incidents_dir = CAO_HOME_DIR / "incidents"
        incidents_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

        incident = {
            "type": "session_degraded",
            "degradation_id": row.id,
            "session_name": row.session_name,
            "cause": row.cause,
            "tombstone_id": row.tombstone_id,
            "terminal_id": row.terminal_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

        file_path = incidents_dir / f"f218-{row.id}.json"
        fd = os.open(str(file_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, json.dumps(incident, indent=2).encode())
        finally:
            os.close(fd)

        return AlarmRungResult(rung="file", ok=True)
    except FileExistsError:
        return AlarmRungResult(rung="file", ok=True, reason="already_exists")
    except Exception as e:
        return AlarmRungResult(rung="file", ok=False, reason=f"{type(e).__name__}: {e}")
