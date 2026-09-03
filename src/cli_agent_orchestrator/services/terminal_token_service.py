"""F332: Per-terminal authentication token service.

Issues tokens at terminal spawn time and verifies them at the inbox endpoint.
Tokens are stored as a column on TerminalModel and compared with constant-time
comparison to prevent timing side-channels.
"""

from __future__ import annotations

import hmac
import secrets
from typing import Optional

from sqlalchemy.orm import Session

from cli_agent_orchestrator.clients.database import TerminalModel


def issue_token(db: Session, terminal_id: str) -> str:
    """Issue a new auth token for a terminal and persist it.

    Must be called within an active DB session that will be committed by the
    caller. Returns the raw token value to be injected into the terminal's
    environment.
    """
    token = secrets.token_urlsafe(32)
    terminal = db.query(TerminalModel).filter_by(id=terminal_id).first()
    if terminal is None:
        raise ValueError(f"Terminal {terminal_id} not found")
    terminal.auth_token = token
    return token


def verify_sender_token(db: Session, sender_id: str, presented: Optional[str]) -> tuple[bool, str]:
    """Verify that the presented token matches the sender's issued token.

    Returns:
        (ok, error_code) — ok=True means pass; error_code is one of
        "E-SENDER-TOKEN" or "E-SENDER-UNKNOWN".
    """
    if not presented:
        # Check if sender exists at all — drives the error code
        terminal = db.query(TerminalModel).filter_by(id=sender_id).first()
        if terminal is None or terminal.auth_token is None:
            return False, "E-SENDER-UNKNOWN"
        return False, "E-SENDER-TOKEN"

    terminal = db.query(TerminalModel).filter_by(id=sender_id).first()
    if terminal is None or terminal.auth_token is None:
        return False, "E-SENDER-UNKNOWN"

    if not hmac.compare_digest(terminal.auth_token, presented):
        return False, "E-SENDER-TOKEN"

    return True, ""
