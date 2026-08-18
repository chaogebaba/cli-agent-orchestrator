"""F295 AC4: Background watcher for canonical grok config changes.

Polls the canonical ``~/.grok/config.toml`` mtime periodically. On change,
recomputes the canonical hash and pushes ONE supervisor inbox notice per
change event. Debounced: one notice per mtime change, not per poll cycle.
NO auto-respawn — flag + notify only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from cli_agent_orchestrator.utils.provider_plane import provider_home

logger = logging.getLogger(__name__)

# Poll interval for mtime checks (seconds).  Cheap stat() call.
GROK_CONFIG_POLL_INTERVAL_S = 10.0


def _canonical_config_path() -> Path:
    """Return the canonical grok config path."""
    return provider_home("grok_cli").home / "config.toml"


def _count_stale_grok_terminals(canonical_hash: str) -> int:
    """Count live grok_cli terminals whose stored config hash differs."""
    from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

    import json as _json

    count = 0
    with SessionLocal() as db:
        terminals = (
            db.query(TerminalModel)
            .filter(TerminalModel.provider == "grok_cli")
            .all()
        )
        for t in terminals:
            if not t.metadata_json:
                continue
            try:
                metadata = _json.loads(t.metadata_json)
            except (ValueError, TypeError):
                continue
            stored = metadata.get("config_sha256")
            if isinstance(stored, str) and stored != canonical_hash:
                count += 1
    return count


def _push_supervisor_notice(canonical_hash: str, stale_count: int) -> bool:
    """Push one inbox notice to the supervisor about config change.

    Returns True on success, False on failure (no supervisor, message error).
    """
    from cli_agent_orchestrator.services.mailbox_service import (
        create_routed_inbox_message,
        get_current_supervisor_terminal_id,
    )

    supervisor_id = get_current_supervisor_terminal_id()
    if not supervisor_id:
        logger.debug("F295 AC4: no active supervisor to notify about config change")
        return False

    message = (
        f"[grok-config-watcher] grok canonical config changed "
        f"(sha256={canonical_hash[:12]}…); "
        f"{stale_count} live grok terminal(s) stale — "
        f"respawn to pick up routing changes."
    )
    try:
        create_routed_inbox_message(
            "cao-system:grok-config-watcher", supervisor_id, message
        )
        logger.info(
            "F295 AC4: pushed config-change notice to supervisor %s (%d stale)",
            supervisor_id,
            stale_count,
        )
        return True
    except Exception as exc:
        logger.warning(
            "F295 AC4: failed to push config-change notice: %s", exc, exc_info=True
        )
        return False


class GrokConfigWatcher:
    """Async background task that polls canonical config mtime and notifies on change."""

    def __init__(self) -> None:
        self._last_mtime: float | None = None
        self._last_hash: str | None = None

    async def run(self) -> None:
        """Main loop — call as an asyncio task from lifespan."""
        logger.info("F295 AC4: grok config watcher started")
        # Initialize baseline mtime without firing a notice
        self._snapshot_baseline()
        while True:
            try:
                await asyncio.sleep(GROK_CONFIG_POLL_INTERVAL_S)
                await asyncio.to_thread(self._check_and_notify)
            except asyncio.CancelledError:
                logger.info("F295 AC4: grok config watcher cancelled")
                break
            except Exception:
                logger.exception("F295 AC4: grok config watcher error")

    def _snapshot_baseline(self) -> None:
        """Record the initial mtime without triggering a notification."""
        try:
            path = _canonical_config_path()
            if path.exists():
                self._last_mtime = path.stat().st_mtime
                text = path.read_text(encoding="utf-8")
                self._last_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            else:
                self._last_mtime = None
                self._last_hash = None
        except OSError:
            self._last_mtime = None
            self._last_hash = None

    def _check_and_notify(self) -> None:
        """Single poll iteration: check mtime, notify if changed."""
        path = _canonical_config_path()
        if not path.exists():
            if self._last_mtime is not None:
                # File was deleted — record state change but don't notify
                # (missing canonical is a warning, not a config change event)
                self._last_mtime = None
                self._last_hash = None
                logger.warning("F295 AC4: canonical config deleted")
            return

        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            return

        # Debounce: only react to mtime changes
        if current_mtime == self._last_mtime:
            return

        # mtime changed — read and hash
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return

        current_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        # Update tracked state
        old_mtime = self._last_mtime
        self._last_mtime = current_mtime

        # If hash unchanged (e.g. touch without edit), skip notification
        if current_hash == self._last_hash:
            return
        self._last_hash = current_hash

        # Push one notice
        stale_count = _count_stale_grok_terminals(current_hash)
        _push_supervisor_notice(current_hash, stale_count)


# Module-level singleton
grok_config_watcher = GrokConfigWatcher()
