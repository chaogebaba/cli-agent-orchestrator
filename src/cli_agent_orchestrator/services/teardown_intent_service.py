"""D16: Durable teardown intent service.

Written BEFORE tmux is touched, removed in a finally.
TTL-bounded so a crashed delete stops suppressing after teardown_intent_ttl_s.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


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
    from cli_agent_orchestrator.clients.database import F218TeardownIntentModel
    from sqlalchemy import or_

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
        db.query(F218TeardownIntentModel).filter(
            F218TeardownIntentModel.expires_at <= now
        ).delete()
        db.commit()
    except Exception:
        db.rollback()

    return False
