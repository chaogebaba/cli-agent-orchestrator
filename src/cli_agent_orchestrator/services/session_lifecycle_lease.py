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


def acquire_session_lifecycle_exclusive_blocking(
    session_name: str,
    *,
    timeout_s: float,
    poll_interval_s: float = 0.25,
) -> SessionLifecycleLeaseToken | None:
    """Acquire the exclusive lifecycle lease, waiting up to ``timeout_s``.

    F513 (#368): the exclusive lease is session-scoped, so a delete of
    terminal X is blocked whenever ANY terminal on the same session holds a
    shared lease — including an unrelated terminal Y's deferred-init
    background task, which holds its shared lease for the full duration of
    ``provider.initialize()`` (up to the F509 watchdog deadline). Rather than
    rescope the lease per-terminal (a large, cross-cutting change to the
    create/delete/rebind mutual-exclusion machinery), give a would-be
    exclusive holder a bounded wait: the common contended holder is a
    transient sibling create/init that releases within seconds, so a short
    poll converts most spurious instant-409s into a successful delete while
    preserving the exclusive-during-teardown invariant.

    Returns the token on success, or ``None`` if the lease could not be
    acquired within ``timeout_s`` (caller maps that to the 409 as before).
    Never blocks the ``_guard`` lock while sleeping — each poll is a plain
    non-blocking ``acquire_session_lifecycle_exclusive`` attempt.
    """
    import time as _time

    deadline = _time.monotonic() + max(0.0, timeout_s)
    interval = max(0.01, poll_interval_s)
    while True:
        token = acquire_session_lifecycle_exclusive(session_name)
        if token is not None:
            return token
        if _time.monotonic() >= deadline:
            return None
        _time.sleep(interval)


def validate_session_lifecycle_shared(
    session_name: str,
    token: SessionLifecycleLeaseToken,
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
