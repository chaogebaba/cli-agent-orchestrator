"""F295 AC4 / F358: Background watcher for canonical grok config changes.

Polls the canonical ``~/.grok/config.toml`` mtime periodically. On change,
extracts ONLY the routing-relevant keys (F358) and pushes ONE supervisor
inbox notice per actual routing change. The grok CLI itself rewrites
config.toml during normal sessions (marketplace/ui/skills churn), so the
raw sha256 is no longer the change detector — the key comparison is.
Debounced: one notice per change event, not per poll cycle.
NO auto-respawn — flag + notify only.

Fail-open (F358): if the TOML fails to parse, notify ONCE (the previous
good state is unknown), then stay quiet until a valid parse resumes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import tomllib
from pathlib import Path

from cli_agent_orchestrator.utils.provider_plane import provider_home

logger = logging.getLogger(__name__)

# Poll interval for mtime checks (seconds).  Cheap stat() call.
GROK_CONFIG_POLL_INTERVAL_S = 10.0

# F358: routing-relevant keys inside the top-level [models] section.
ROUTING_MODELS_KEYS = ("default", "default_reasoning_effort")

# F358: top-level sections that carry endpoint/api routing wholesale.
# [model.<name>] tables hold base_url / api_key / api_backend (see
# grok_preflight._read_model_table); [api] is included when present.
ROUTING_SECTIONS = ("model", "api")


def _routing_fingerprint(text: str) -> dict | None:
    """F358: extract routing-relevant keys from config TOML text.

    Returns a comparable dict of routing keys, or None when the TOML is
    malformed (caller fails open).
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    fp: dict = {}
    models = data.get("models")
    if isinstance(models, dict):
        picked = {k: models[k] for k in ROUTING_MODELS_KEYS if k in models}
        if picked:
            fp["models"] = picked
    for section in ROUTING_SECTIONS:
        val = data.get(section)
        if isinstance(val, dict) and val:
            fp[section] = val
    return fp


def _canonical_config_path() -> Path:
    """Return the canonical grok config path."""
    return provider_home("grok_cli").home / "config.toml"


def _count_stale_grok_terminals(canonical_hash: str) -> int:
    """Count live grok_cli terminals whose stored config hash differs."""
    from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

    count = 0
    with SessionLocal() as db:
        terminals = db.query(TerminalModel).filter(TerminalModel.provider == "grok_cli").all()
        for t in terminals:
            if not t.metadata_json:
                continue
            try:
                metadata = _json.loads(str(t.metadata_json))
            except (ValueError, TypeError):
                continue
            # D12: read from reserved 'cao' namespace, with legacy top-level fallback (AC13)
            stored = None
            cao_ns = metadata.get("cao") if isinstance(metadata, dict) else None
            if isinstance(cao_ns, dict):
                stored = cao_ns.get("config_sha256")
            if not isinstance(stored, str):
                stored = metadata.get("config_sha256") if isinstance(metadata, dict) else None
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
        create_routed_inbox_message("cao-system:grok-config-watcher", supervisor_id, message)
        logger.info(
            "F295 AC4: pushed config-change notice to supervisor %s (%d stale)",
            supervisor_id,
            stale_count,
        )
        return True
    except Exception as exc:
        logger.warning("F295 AC4: failed to push config-change notice: %s", exc, exc_info=True)
        return False


class GrokConfigWatcher:
    """Async background task that polls canonical config mtime and notifies on change."""

    def __init__(self) -> None:
        self._last_mtime: float | None = None
        self._last_hash: str | None = None
        # F358: routing-key fingerprint is the change detector; hash stays
        # in the notice text and stale-count comparison only.
        self._last_fingerprint: dict | None = None
        self._parse_fail_notified: bool = False

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
                # F358: baseline the routing fingerprint; a malformed
                # baseline suppresses the fail-open notice (nothing to
                # compare against — no change event has occurred).
                fingerprint = _routing_fingerprint(text)
                self._last_fingerprint = fingerprint
                self._parse_fail_notified = fingerprint is None
            else:
                self._last_mtime = None
                self._last_hash = None
                self._last_fingerprint = None
                self._parse_fail_notified = False
        except OSError:
            self._last_mtime = None
            self._last_hash = None
            self._last_fingerprint = None
            self._parse_fail_notified = False

    def _check_and_notify(self) -> None:
        """Single poll iteration: check mtime, notify if changed."""
        path = _canonical_config_path()
        if not path.exists():
            if self._last_mtime is not None:
                # File was deleted — record state change but don't notify
                # (missing canonical is a warning, not a config change event)
                self._last_mtime = None
                self._last_hash = None
                # Keep _last_fingerprint so a resurrected config is compared
                # against the pre-deletion routing state; re-arm fail-open.
                self._parse_fail_notified = False
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
        self._last_mtime = current_mtime

        # If hash unchanged (e.g. touch without edit), skip notification
        if current_hash == self._last_hash:
            return

        # F358 fail-open: malformed TOML — notify once, then stay quiet
        # until a valid parse resumes.
        fingerprint = _routing_fingerprint(text)
        if fingerprint is None:
            if not self._parse_fail_notified:
                self._parse_fail_notified = True
                self._last_hash = current_hash
                logger.warning("F358: grok config TOML parse failed; failing open (one notice)")
                stale_count = _count_stale_grok_terminals(current_hash)
                _push_supervisor_notice(current_hash, stale_count)
            return
        self._parse_fail_notified = False

        self._last_hash = current_hash

        # F358: the change detector is the routing-key comparison, NOT the
        # sha256 — the grok CLI rewrites non-routing sections during normal
        # sessions and those churns must not fire "respawn" notices.
        if fingerprint == self._last_fingerprint:
            return
        self._last_fingerprint = fingerprint

        # Push one notice
        stale_count = _count_stale_grok_terminals(current_hash)
        _push_supervisor_notice(current_hash, stale_count)


# Module-level singleton
grok_config_watcher = GrokConfigWatcher()
