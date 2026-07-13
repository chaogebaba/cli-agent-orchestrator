"""Process-local shared-intent/exclusive leases for session lifecycle mutation."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionLifecycleLeaseToken:
    session_name: str
    mode: str
    nonce: str


_guard = threading.Lock()
_shared: dict[str, set[SessionLifecycleLeaseToken]] = {}
_exclusive: dict[str, SessionLifecycleLeaseToken] = {}


def acquire_session_lifecycle_shared(session_name: str) -> SessionLifecycleLeaseToken | None:
    with _guard:
        if session_name in _exclusive:
            return None
        token = SessionLifecycleLeaseToken(session_name, "shared", uuid.uuid4().hex)
        _shared.setdefault(session_name, set()).add(token)
        return token


def acquire_session_lifecycle_exclusive(session_name: str) -> SessionLifecycleLeaseToken | None:
    with _guard:
        if session_name in _exclusive or _shared.get(session_name):
            return None
        token = SessionLifecycleLeaseToken(session_name, "exclusive", uuid.uuid4().hex)
        _exclusive[session_name] = token
        return token


def validate_session_lifecycle_shared(
    session_name: str, token: SessionLifecycleLeaseToken,
) -> None:
    with _guard:
        if token.mode != "shared" or token not in _shared.get(session_name, set()):
            raise RuntimeError("invalid_session_lifecycle_lease_token")


def release_session_lifecycle_lease(token: SessionLifecycleLeaseToken) -> None:
    with _guard:
        if token.mode == "exclusive" and _exclusive.get(token.session_name) == token:
            del _exclusive[token.session_name]
            return
        shared = _shared.get(token.session_name)
        if token.mode == "shared" and shared and token in shared:
            shared.remove(token)
            if not shared:
                del _shared[token.session_name]
            return
        raise RuntimeError("invalid_session_lifecycle_lease_token")
