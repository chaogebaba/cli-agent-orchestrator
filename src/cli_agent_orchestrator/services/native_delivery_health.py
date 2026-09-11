"""Is the seat's NATIVE channel reachable, and where does its socket live?

WP-ARCH 3c K2 deletes ``teammate_push_service``, which held two unrelated things
in one file: the legacy pull-mode FILE carrier (a JSON inbox the seat polled, its
lockfile, its writer, its flags, its reconciler) and this — the probe that answers
whether the seat's NATIVE cross-session channel can be reached at all, plus the
derivation of the socket path itself.

**The second half is not K2.** ``cc_team_inbox_path`` is the native socket the
register hook publishes and the queue's ``NativeSeatCarrier`` writes to; it only
ever shared a file with the legacy carrier. Deleting it with K2 would take the
native path's address derivation with the legacy path's file writer, which is the
opposite of what the phase is doing. So it moves here, unchanged.

**What the health probe is FOR.** The seat's residual fallback — the ``CAO
callback waiting`` task-notification armed by the rewake hook — asks before it
surfaces anything. ``healthy: true`` means the native channel is the only surface
and the fallback must stay silent. ``healthy: false`` names which of a CLOSED set
broke, so an engagement is a filed condition rather than an invisible default.

**Two reasons left the set in 3c**, because both named flags that no longer
exist: ``push_disabled_by_operator`` (``supervisor.teammate_push``) and
``no_native_driver`` (``supervisor.mailbox_pull``, which gated the deleted
pull-mode reconciler). Neither can be true of a native channel — they were
questions about the legacy pusher — and a reason that cannot occur is a filter an
operator can select and never match.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional

from cli_agent_orchestrator.clients.database import get_terminal_metadata

logger = logging.getLogger(__name__)

__all__ = [
    "NATIVE_FALLBACK_REASONS",
    "clear_native_write_failure",
    "derive_cc_team_inbox_path",
    "log_native_fallback_engaged",
    "native_fallback_reason",
    "record_native_write_failure",
    "resolve_inbox_path",
]

#: The closed set of reasons the residual fallback may engage. Closed on purpose:
#: every engagement has to name one, and a probe that could answer "something
#: broke" would be the invisible default this replaced.
NATIVE_FALLBACK_REASONS = (
    "provider_not_native",
    "no_inbox_path",
    "native_write_failed",
)

#: How long a native write failure keeps the fallback armed for a terminal.
NATIVE_WRITE_FAILURE_TTL_S = 300.0

#: Rate limit for the engagement WARN.
NATIVE_FALLBACK_WARN_INTERVAL_S: float = 60.0

#: terminal_id -> monotonic ts of the last failed native write.
_native_write_failures: Dict[str, float] = {}

#: terminal_id -> monotonic ts of the last engagement WARN.
_native_fallback_last_warn: Dict[str, float] = {}


def record_native_write_failure(terminal_id: str) -> None:
    """Arm the fallback for ``terminal_id`` after a failed native write."""
    _native_write_failures[terminal_id] = time.monotonic()


def clear_native_write_failure(terminal_id: str) -> None:
    """Disarm the fallback after a native write succeeds."""
    _native_write_failures.pop(terminal_id, None)


def _has_recent_native_write_failure(terminal_id: str) -> bool:
    ts = _native_write_failures.get(terminal_id)
    if ts is None:
        return False
    if (time.monotonic() - ts) >= NATIVE_WRITE_FAILURE_TTL_S:
        _native_write_failures.pop(terminal_id, None)
        return False
    return True


def log_native_fallback_engaged(terminal_id: str, reason: str) -> bool:
    """Emit one rate-limited WARNING per fallback engagement. True if emitted."""
    now_ts = time.monotonic()
    last = _native_fallback_last_warn.get(terminal_id)
    if last is not None and (now_ts - last) < NATIVE_FALLBACK_WARN_INTERVAL_S:
        return False
    _native_fallback_last_warn[terminal_id] = now_ts
    logger.warning("native_fallback_engaged terminal=%s reason=%s", terminal_id, reason)
    return True


def native_fallback_reason(terminal_id: str) -> Optional[str]:
    """Why the residual fallback surface may engage, or ``None`` when healthy."""
    metadata = get_terminal_metadata(terminal_id)
    if not metadata or metadata.get("provider") != "claude_code":
        return "provider_not_native"
    # ``persist=False`` keeps this a pure read. The probe runs on the seat's
    # PostToolUse edge -- once per tool call per seat -- so a metadata write here
    # would turn a read-only question into a DB write on the hottest path in the
    # system, serialising every caller behind the same SQLite lock.
    if resolve_inbox_path(terminal_id, persist=False) is None:
        return "no_inbox_path"
    if _has_recent_native_write_failure(terminal_id):
        return "native_write_failed"
    return None


def resolve_inbox_path(terminal_id: str, *, persist: bool = True) -> Optional[Path]:
    """Resolve and expand the seat's native socket path from terminal metadata.

    WPDT W3 (F152): includes the lazy-derive self-heal — if ``cc_team_inbox_path``
    is absent but the terminal has a working_directory and is a claude_code
    provider, derive the path and persist it for future calls.
    """
    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        return None
    md = metadata.get("metadata") or {}
    raw = md.get("cc_team_inbox_path")
    if raw:
        return Path(os.path.expanduser(raw))

    # F152 self-heal: derive from working_directory + provider.
    #
    # F747 (#747) tried an ``os.getcwd()`` fallback here so a row with an empty
    # ``working_directory`` could still resolve. That was wrong twice over.
    # Correctness: every terminal without a recorded cwd would derive the SAME
    # path (the server's cwd), so unrelated seats would share one file and
    # serialise on its lockfile -- measured as a two-order-of-magnitude suite
    # slowdown. Design: a terminal with no cwd is precisely the "native cannot
    # work here" case, and the typed ``no_inbox_path`` reason is the designed
    # answer to it, not an invented shared path.
    provider = metadata.get("provider")
    working_dir = metadata.get("working_directory")
    if not working_dir or provider != "claude_code":
        return None

    derived = derive_cc_team_inbox_path(working_dir)
    if derived is None:
        return None

    if not persist:
        return derived
    try:
        from cli_agent_orchestrator.clients.database import update_terminal_metadata

        new_md = dict(md)
        new_md["cc_team_inbox_path"] = str(derived)
        update_terminal_metadata(terminal_id, new_md)
        logger.info("f152_self_heal terminal=%s path=%s", terminal_id, derived)
    except Exception as e:
        logger.debug("f152_self_heal persist failed: %s", e)

    return derived


def derive_cc_team_inbox_path(working_directory: str) -> Optional[Path]:
    """F152: derive the seat's native socket path from a working directory.

    Uses the Claude provider's project-directory encoding:
    ``~/.claude/projects/{cwd_key}/team-lead.json``.
    """
    import re

    try:
        cwd_key = re.sub(r"[^A-Za-z0-9]", "-", working_directory)
        # F747 (#747): derivation is a PURE function of the cwd -- no mkdir. It
        # runs for every claude_code terminal create and every health probe, so a
        # filesystem side effect here would fire on both.
        return Path.home() / ".claude" / "projects" / cwd_key / "team-lead.json"
    except Exception:
        return None
