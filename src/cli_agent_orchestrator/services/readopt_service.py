"""F335: Registry self-heal — re-adopt live tmux panes when terminal rows vanish.

Scans the host tmux server for cao-* sessions whose windows are named
``<profile>-<terminalid8hex>``; for each id missing from the ``terminals``
table, reconstructs the minimal registry rows:
- terminals row (id, tmux_session, tmux_window, provider, agent_profile,
  lifecycle, init_state=ready)
- supervisor mailbox + mailbox_incarnations link (when the profile role is
  "supervisor")

DRY-RUN by default; ``--apply`` executes the inserts. Never touches rows
that already exist. Skips sessions matching the test prefix ``cao-test-*``.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

logger = logging.getLogger(__name__)

# Test-session prefix to skip (wp-callbacks convention: fixture sessions
# use "cao-test-" prefix so they never collide with production)
_TEST_SESSION_PREFIX = "cao-test-"

# Pattern: window name is <profile>-<8-char hex terminal id>
_WINDOW_NAME_RE = re.compile(r"^(.+)-([0-9a-f]{8})$")

# Profiles with these roles get a supervisor mailbox
_SUPERVISOR_ROLES = frozenset({"supervisor"})


@dataclass(frozen=True)
class ReadoptPlan:
    """One terminal that would be re-adopted."""

    terminal_id: str
    tmux_session: str
    tmux_window: str
    agent_profile: str
    provider: str
    lifecycle: str
    needs_mailbox: bool


@dataclass
class ReadoptResult:
    """Aggregate result of a readopt scan/apply."""

    planned: list[ReadoptPlan] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    skipped_test: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _list_tmux_windows() -> list[tuple[str, str]]:
    """Return (session_name, window_name) for all windows on the tmux server.

    Uses ``tmux list-windows -a -F '#{session_name}:#{window_name}'``.
    Returns empty list if tmux is not running or no sessions exist.
    """
    try:
        output = subprocess.check_output(
            ["tmux", "list-windows", "-a", "-F", "#{session_name}\t#{window_name}"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []

    results: list[tuple[str, str]] = []
    for line in output.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            results.append((parts[0], parts[1]))
    return results


def _resolve_provider_for_profile(profile_name: str) -> str | None:
    """Resolve the provider for a profile name.

    Returns None when the profile cannot be loaded at all (file missing,
    parse error) — the caller must skip the terminal rather than record a
    wrong provider that delete_terminal's provider-specific cleanup would
    later act on.
    """
    try:
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        profile = load_agent_profile(profile_name)
        if profile.provider:
            from cli_agent_orchestrator.constants import PROVIDERS

            if profile.provider in PROVIDERS:
                return profile.provider
        # Profile loaded but has no explicit provider — use default
        from cli_agent_orchestrator.constants import DEFAULT_PROVIDER

        return DEFAULT_PROVIDER
    except (FileNotFoundError, RuntimeError, ValueError):
        # Profile cannot be resolved — caller must skip this terminal
        return None


def _resolve_role_for_profile(profile_name: str) -> str | None:
    """Resolve the role from a profile's frontmatter. Returns None on failure."""
    try:
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        profile = load_agent_profile(profile_name)
        return getattr(profile, "role", None)
    except Exception:
        return None


def _is_supervisor_profile(profile_name: str) -> bool:
    """Return True if the profile's role is a supervisor role."""
    role = _resolve_role_for_profile(profile_name)
    return role in _SUPERVISOR_ROLES


def _existing_terminal_ids(db) -> set[str]:
    """Return the set of terminal IDs currently in the database."""
    from cli_agent_orchestrator.clients.database import TerminalModel

    rows = db.query(TerminalModel.id).all()
    return {r[0] for r in rows}


def scan_for_orphans(
    *, tmux_windows: list[tuple[str, str]] | None = None
) -> ReadoptResult:
    """Scan tmux for cao-* session windows missing from the terminals table.

    Args:
        tmux_windows: Optional pre-fetched window list (for testing).
            If None, queries the live tmux server.

    Returns:
        ReadoptResult with planned readoptions (dry-run; nothing written).
    """
    from cli_agent_orchestrator.clients.database import SessionLocal

    if tmux_windows is None:
        tmux_windows = _list_tmux_windows()

    result = ReadoptResult()

    # Filter to cao-* sessions only
    cao_windows: list[tuple[str, str]] = []
    for sess, win in tmux_windows:
        if not sess.startswith("cao-"):
            continue
        if sess.startswith(_TEST_SESSION_PREFIX):
            result.skipped_test.append(f"{sess}:{win}")
            continue
        cao_windows.append((sess, win))

    if not cao_windows:
        return result

    with SessionLocal() as db:
        existing_ids = _existing_terminal_ids(db)

    for sess, win in cao_windows:
        m = _WINDOW_NAME_RE.match(win)
        if m is None:
            continue  # Not a CAO-managed window name

        profile_name = m.group(1)
        terminal_id = m.group(2)

        if terminal_id in existing_ids:
            result.skipped_existing.append(terminal_id)
            continue

        provider = _resolve_provider_for_profile(profile_name)
        if provider is None:
            # D4: cannot resolve provider truthfully — skip with warning
            msg = (
                f"skipped {terminal_id} (session={sess}): "
                f"profile '{profile_name}' not found or invalid — "
                f"cannot determine provider"
            )
            result.errors.append(msg)
            logger.warning("f335_readopt_skip_unresolvable: %s", msg)
            continue

        is_supervisor = _is_supervisor_profile(profile_name)
        lifecycle = "sticky" if is_supervisor else "ephemeral"

        result.planned.append(
            ReadoptPlan(
                terminal_id=terminal_id,
                tmux_session=sess,
                tmux_window=win,
                agent_profile=profile_name,
                provider=provider,
                lifecycle=lifecycle,
                needs_mailbox=is_supervisor,
            )
        )

    return result


def apply_readopt(result: ReadoptResult) -> ReadoptResult:
    """Execute the planned readoptions — insert terminal + mailbox rows.

    Mutates ``result.applied`` and ``result.errors`` in place.
    Never touches rows that already exist (uses INSERT OR IGNORE semantics).
    """
    from cli_agent_orchestrator.clients.database import (
        MailboxIncarnationModel,
        MailboxModel,
        SessionLocal,
        TerminalModel,
    )

    now = datetime.now(timezone.utc)

    for plan in result.planned:
        try:
            with SessionLocal.begin() as db:
                # Double-check: skip if row appeared between scan and apply
                exists = (
                    db.query(TerminalModel.id)
                    .filter_by(id=plan.terminal_id)
                    .first()
                )
                if exists:
                    result.skipped_existing.append(plan.terminal_id)
                    continue

                # Insert terminal row
                terminal = TerminalModel(
                    id=plan.terminal_id,
                    tmux_session=plan.tmux_session,
                    tmux_window=plan.tmux_window,
                    provider=plan.provider,
                    agent_profile=plan.agent_profile,
                    lifecycle=plan.lifecycle,
                    init_state="ready",
                    last_active=now,
                )
                db.add(terminal)

                # Insert supervisor mailbox + incarnation if needed
                if plan.needs_mailbox:
                    mailbox_id = f"mb_{plan.terminal_id}"
                    mailbox = MailboxModel(
                        id=mailbox_id,
                        session_name=plan.tmux_session,
                        role="supervisor",
                        current_terminal_id=plan.terminal_id,
                        generation=1,
                        consumed_through_id=0,
                        schema_version=1,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(mailbox)

                    incarnation = MailboxIncarnationModel(
                        mailbox_id=mailbox_id,
                        generation=1,
                        terminal_id=plan.terminal_id,
                        published_at=now,
                    )
                    db.add(incarnation)

                result.applied.append(plan.terminal_id)
                logger.info(
                    "f335_readopt: adopted terminal=%s session=%s profile=%s mailbox=%s",
                    plan.terminal_id,
                    plan.tmux_session,
                    plan.agent_profile,
                    plan.needs_mailbox,
                )
        except Exception as e:
            msg = f"terminal={plan.terminal_id}: {type(e).__name__}: {e}"
            result.errors.append(msg)
            logger.error("f335_readopt_error: %s", msg)

    return result
