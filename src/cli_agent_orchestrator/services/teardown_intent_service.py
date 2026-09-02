"""D16: Durable teardown intent service.

Written BEFORE tmux is touched, removed in a finally.
TTL-bounded so a crashed delete stops suppressing after teardown_intent_ttl_s.
"""

import logging
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# F716 (#571) r2: in-process teardown marks — the fallback that makes "a mark
# always exists before a tmux kill" unconditional.
#
# The durable DB intent is still primary (it survives a process restart and is
# visible to any reader of the database). But `open_intent` can fail — a locked
# SQLite file, a migration gap, a disk error — and `delete_terminal` must NOT be
# blocked by that: a delete is itself a recovery operation, and refusing to reap
# a terminal because a bookkeeping table is unavailable would turn a cosmetic
# fleet-projection defect into an availability defect. So the delete path also
# sets a process-local mark, which `active_teardown_scope_keys` unions into the
# result. Same TTL discipline as the DB row: a crashed delete stops suppressing.
_MEMORY_MARKS: dict[str, float] = {}
_MEMORY_MARKS_LOCK = threading.Lock()


def mark_teardown(scope_key: str, ttl_s: float = 300.0) -> None:
    """Set a process-local teardown mark for ``scope_key`` (terminal id or session).

    Cheap, never raises, and independent of the database. Callers set this
    BEFORE any tmux kill and clear it in a ``finally``.
    """
    with _MEMORY_MARKS_LOCK:
        _MEMORY_MARKS[scope_key] = time.monotonic() + ttl_s


def unmark_teardown(scope_key: str) -> None:
    """Clear a process-local teardown mark. Idempotent; never raises."""
    with _MEMORY_MARKS_LOCK:
        _MEMORY_MARKS.pop(scope_key, None)


def _active_memory_marks() -> set[str]:
    """Unexpired process-local marks, dropping expired entries as it goes."""
    now = time.monotonic()
    with _MEMORY_MARKS_LOCK:
        for key in [k for k, deadline in _MEMORY_MARKS.items() if deadline <= now]:
            del _MEMORY_MARKS[key]
        return set(_MEMORY_MARKS)


def open_intent(
    *,
    scope_kind: str,
    scope_key: str,
    requested_by: str | None = None,
    ttl_s: float = 300.0,
    db: Session,
) -> str:
    """Open a teardown intent. COMMIT before returning (D16: before any tmux call)."""
    from cli_agent_orchestrator.clients.database import F218TeardownIntentModel

    intent_id = uuid.uuid4().hex[:16]
    now = datetime.now(timezone.utc)

    row = F218TeardownIntentModel(
        id=intent_id,
        scope_kind=scope_kind,
        scope_key=scope_key,
        requested_by=requested_by,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_s),
    )

    try:
        db.add(row)
        db.commit()
        logger.info(
            "f218_teardown scope=%s key=%s action=open ttl=%s",
            scope_kind,
            scope_key,
            ttl_s,
        )
        return intent_id
    except IntegrityError:
        db.rollback()
        # Already exists — update the expiry (extend TTL for the new request)
        from sqlalchemy import text as sa_text

        db.execute(
            sa_text(
                "UPDATE f218_teardown_intents SET expires_at = :expires, requested_by = :by "
                "WHERE scope_kind = :kind AND scope_key = :key"
            ),
            {
                "expires": (now + timedelta(seconds=ttl_s)).isoformat(),
                "by": requested_by,
                "kind": scope_kind,
                "key": scope_key,
            },
        )
        db.commit()
        # Return the existing id
        existing = db.execute(
            sa_text(
                "SELECT id FROM f218_teardown_intents WHERE scope_kind = :kind AND scope_key = :key"
            ),
            {"kind": scope_kind, "key": scope_key},
        ).fetchone()
        return existing[0] if existing else intent_id


def close_intent(intent_id: str, db: Session) -> None:
    """Close (delete) a teardown intent. Called in `finally`."""
    from cli_agent_orchestrator.clients.database import F218TeardownIntentModel

    try:
        db.query(F218TeardownIntentModel).filter_by(id=intent_id).delete()
        db.commit()
        logger.info("f218_teardown action=close id=%s", intent_id)
    except Exception as e:
        db.rollback()
        logger.warning("f218_teardown_close_failed id=%s: %s", intent_id, e)


def is_teardown_intended(
    *,
    session_name: str,
    terminal_id: str | None = None,
    db: Session,
) -> bool:
    """Check if a teardown intent exists and is unexpired.

    True iff an unexpired row matches scope=(session, session_name)
    OR scope=(terminal, terminal_id). Expired rows match nothing and are
    deleted lazily.
    """
    from sqlalchemy import or_

    from cli_agent_orchestrator.clients.database import F218TeardownIntentModel

    now = datetime.now(timezone.utc)

    conditions = [
        (F218TeardownIntentModel.scope_kind == "session")
        & (F218TeardownIntentModel.scope_key == session_name)
        & (F218TeardownIntentModel.expires_at > now)
    ]
    if terminal_id:
        conditions.append(
            (F218TeardownIntentModel.scope_kind == "terminal")
            & (F218TeardownIntentModel.scope_key == terminal_id)
            & (F218TeardownIntentModel.expires_at > now)
        )

    exists = db.query(F218TeardownIntentModel).filter(or_(*conditions)).first()

    if exists:
        return True

    # Lazy cleanup: delete expired rows we encountered
    try:
        db.query(F218TeardownIntentModel).filter(F218TeardownIntentModel.expires_at <= now).delete()
        db.commit()
    except Exception:
        db.rollback()

    return False


def active_teardown_scope_keys() -> set[str]:
    """F716 (#571): scope_keys of ALL unexpired teardown intents.

    Terminal-scope and session-scope keys in one set, so a fleet projection
    can distinguish "window gone because we are deleting it on purpose"
    (intent open — delete_terminal opens the intent BEFORE killing the
    window) from "window gone unexpectedly" (no intent). TTL-bounded like
    :func:`is_teardown_intended`, so a crashed delete stops suppressing.

    r2: the durable rows are UNIONED with the in-process marks set by
    :func:`mark_teardown`. A database read failure degrades to the marks
    alone rather than to the empty set, because the case that most needs
    the fallback (the DB is unhealthy, so ``open_intent`` failed) is
    exactly the case where this query fails too.
    """
    from cli_agent_orchestrator.clients.database import (
        F218TeardownIntentModel,
        SessionLocal,
    )

    keys = _active_memory_marks()

    now = datetime.now(timezone.utc)
    try:
        with SessionLocal() as db:
            rows = (
                db.query(F218TeardownIntentModel.scope_key)
                .filter(
                    F218TeardownIntentModel.scope_kind.in_(("terminal", "session")),
                    F218TeardownIntentModel.expires_at > now,
                )
                .all()
            )
    except Exception as e:
        logger.warning("f218_teardown_scope_keys_db_read_failed (using in-process marks): %s", e)
        return keys
    return keys | {row[0] for row in rows}


@contextmanager
def teardown_bracket(
    scope_key: str,
    *,
    scope_kind: str = "terminal",
    requested_by: str | None = None,
    ttl_s: float | None = None,
) -> Iterator[None]:
    """F720 (#576): the F716 mark -> open -> (tmux kill) -> close -> unmark bracket.

    ``delete_terminal`` open-codes this sequence (terminal_service.py, F716
    #571). Every OTHER tmux kill site needs the same protection, and open-coding
    it five more times is how the sites drift apart, so the sequence lives here
    and the sites say ``with teardown_bracket(...)``.

    Ordering is the whole point and matches F716 exactly: the in-process mark
    goes first and cannot fail, so no kill inside the block can ever run
    without SOME mark; the durable intent is opened right after and stays
    authoritative across processes; a failure to open it is logged and the
    block proceeds on the mark alone rather than blocking a teardown on a
    bookkeeping table.

    ``scope_kind="session"`` marks the SESSION name, which
    ``fleet_service.build_fleet`` matches against every row of that session
    (fleet_service.py: ``session_name in teardown_scope_keys``). Use it only
    where the whole session is being killed -- a session-scope mark taken for
    a single terminal's sake would also suppress the ERROR of live peers on
    that session, which is the AC-F716-2 safety property.

    Nesting the same key is tolerated: the bracket releases the in-process mark
    only if it was the one that set it. The durable row is shared by key
    (``open_intent`` returns the existing id), so an inner close removes the
    outer's row and leaves it running on the in-process mark alone; no site
    pairs this way today.
    """
    from cli_agent_orchestrator.clients.database import SessionLocal
    from cli_agent_orchestrator.services.config_service import ConfigService

    resolved_ttl = (
        float(ConfigService.get("teardown.intent_ttl_s", 300.0)) if ttl_s is None else ttl_s
    )
    already_marked = scope_key in _active_memory_marks()
    mark_teardown(scope_key, ttl_s=resolved_ttl)
    intent_id: str | None = None
    try:
        with SessionLocal() as db:
            intent_id = open_intent(
                scope_kind=scope_kind,
                scope_key=scope_key,
                requested_by=requested_by,
                ttl_s=resolved_ttl,
                db=db,
            )
    except Exception as e:
        logger.warning(
            "f218_teardown_intent_open_failed scope=%s key=%s (in-process mark still set): %s",
            scope_kind,
            scope_key,
            e,
        )
    try:
        yield
    finally:
        if intent_id is not None:
            try:
                with SessionLocal() as db:
                    close_intent(intent_id, db)
            except Exception as e:
                logger.warning("f218_teardown_intent_close_failed scope_key=%s: %s", scope_key, e)
        if not already_marked:
            unmark_teardown(scope_key)
