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


@dataclass(frozen=True)
class TerminalLifecycleLeaseToken:
    """F867 (#723) D2: a terminal-scoped exclusive teardown lease token."""

    session_name: str
    terminal_id: str
    nonce: str


_guard = threading.Lock()
_shared: dict[str, set[SessionLifecycleLeaseToken]] = {}
_exclusive: dict[str, SessionLifecycleLeaseToken] = {}
# F867 (#723) D2: terminal-scoped exclusive teardown leases, keyed by
# ``(session_name, terminal_id)``. A per-terminal ``delete_terminal`` takes one
# of these instead of the SESSION-wide exclusive so it is NOT blocked by shared
# leases held for OTHER terminals (a sibling create/resume in flight on the same
# session). It STILL conflicts with a full session teardown (the session-wide
# exclusive) in BOTH directions, so a terminal delete and a session close never
# run concurrently.
_terminal_exclusive: dict[tuple[str, str], TerminalLifecycleLeaseToken] = {}


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
        # F867 (#723) D2: a SESSION teardown must also wait for any in-flight
        # TERMINAL-scoped teardown on the same session (symmetric mutual
        # exclusion), so a session close never races a per-terminal delete.
        if any(key[0] == session_name for key in _terminal_exclusive):
            return None
        token = SessionLifecycleLeaseToken(session_name, "exclusive", uuid.uuid4().hex)
        _exclusive[session_name] = token
        return token


def acquire_session_lifecycle_terminal_exclusive(
    session_name: str, terminal_id: str
) -> "TerminalLifecycleLeaseToken | None":
    """F867 (#723) D2: acquire a TERMINAL-scoped exclusive teardown lease.

    Unlike the session-wide exclusive, this is NOT blocked by shared leases held
    for OTHER terminals (a sibling create/resume in flight) — that session-wide
    contention was the sole cause of the #723 409 storm, where a completed idle
    lane could not be force-reaped because unrelated siblings kept a session
    shared lease. It DOES conflict with:

    * the SESSION-wide exclusive (a full session teardown in progress), and
    * another terminal-exclusive for the SAME ``(session, terminal_id)``.

    It deliberately does NOT gate on a resume/rebind of THIS terminal — that is
    enforced separately by the per-terminal rebind lease and the provider-session
    lease (see ``_delete_terminal_inner``), so this token stays a pure teardown
    mutex.
    """
    with _guard:
        if session_name in _exclusive:
            return None
        key = (session_name, terminal_id)
        if key in _terminal_exclusive:
            return None
        token = TerminalLifecycleLeaseToken(session_name, terminal_id, uuid.uuid4().hex)
        _terminal_exclusive[key] = token
        return token


def acquire_session_lifecycle_terminal_exclusive_blocking(
    session_name: str,
    terminal_id: str,
    *,
    timeout_s: float,
    poll_interval_s: float = 0.25,
) -> "TerminalLifecycleLeaseToken | None":
    """F867 (#723) D2: bounded-wait acquire of the terminal-scoped teardown lease.

    Mirrors ``acquire_session_lifecycle_exclusive_blocking`` but for the
    per-terminal token: waits up to ``timeout_s`` for a concurrent SESSION
    teardown (or a sibling delete of the SAME terminal) to finish, then returns
    the token or ``None``. It never waits on shared leases held for other
    terminals, so the common #723 contention resolves immediately.
    """
    import time as _time

    deadline = _time.monotonic() + max(0.0, timeout_s)
    interval = max(0.01, poll_interval_s)
    while True:
        token = acquire_session_lifecycle_terminal_exclusive(session_name, terminal_id)
        if token is not None:
            return token
        if _time.monotonic() >= deadline:
            return None
        _time.sleep(interval)


def release_session_lifecycle_terminal_exclusive(token: "TerminalLifecycleLeaseToken") -> None:
    """F867 (#723) D2: release a terminal-scoped exclusive teardown lease."""
    with _guard:
        key = (token.session_name, token.terminal_id)
        if _terminal_exclusive.get(key) == token:
            del _terminal_exclusive[key]
            return
        raise RuntimeError("invalid_terminal_lifecycle_lease_token")


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
