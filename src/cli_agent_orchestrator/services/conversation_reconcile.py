"""F829 D8: conversation-root reconciliation (startup + stale-claim sweeps).

Two idempotent sweeps, both built ONLY on tested DB primitives and the existing
authoritative death signal (a ``PaneExitTombstoneModel`` row, via
``delivery_service.is_target_confirmed_dead`` — a DB-only check, no tmux call):

* ``reconcile_live_roots`` (N1): at server startup, every conversation root in
  lifecycle ``live`` is checked against the tombstone. A root whose
  ``current_terminal_id`` is CONFIRMED dead is crash-detached (the D8 narrow
  transition → ``detached``); a root whose terminal is alive, or for which death
  is merely UNCONFIRMED (no tombstone — e.g. a tmux/server probe failure), is
  left ``live`` and re-checked on the next boot. Death is never inferred from
  uncertainty (D8).

* ``reconcile_stale_claims``: before any new resume, a resume claim older than
  ``resume.claim_ttl_s`` (default 600) is cleared with an event, so an
  interrupted resume/detach generation cannot wedge a conversation forever.

Neither sweep cancels barriers, flips membership GONE, or touches frozen pins —
those remain the #299 dependency named in D8. Nothing here raises into the
server lifespan.
"""

from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger(__name__)

DEFAULT_CLAIM_TTL_S = 600.0


def reconcile_stale_claims() -> List[str]:
    """Clear resume claims older than ``resume.claim_ttl_s``. Returns cleared keys."""
    from cli_agent_orchestrator.clients.database import reconcile_stale_resume_claims

    ttl = DEFAULT_CLAIM_TTL_S
    try:
        from cli_agent_orchestrator.services.config_service import ConfigService

        ttl = float(ConfigService.get("resume.claim_ttl_s", DEFAULT_CLAIM_TTL_S))
    except Exception:
        logger.debug("resume.claim_ttl_s config read failed; using default", exc_info=True)
    try:
        return reconcile_stale_resume_claims(ttl)
    except Exception:
        logger.exception("reconcile_stale_claims failed")
        return []


def reconcile_live_roots() -> dict:
    """N1 startup sweep: crash-detach every ``live`` root whose terminal is
    CONFIRMED dead; leave the rest ``live``.

    Returns a summary ``{checked, detached, left_live}``. Idempotent: a root
    already ``detached`` is not ``live`` and so is never revisited, and a second
    run over a still-alive fleet is a no-op.
    """
    from cli_agent_orchestrator.clients.database import (
        SessionLocal,
        crash_detach_terminal,
        list_live_conversation_roots,
    )
    from cli_agent_orchestrator.services.delivery_service import is_target_confirmed_dead

    checked = 0
    detached = 0
    left_live = 0
    try:
        roots = list_live_conversation_roots()
    except Exception:
        logger.exception("reconcile_live_roots: could not list live roots")
        return {"checked": 0, "detached": 0, "left_live": 0}

    for root in roots:
        checked += 1
        terminal_id = root.get("current_terminal_id")
        if not terminal_id:
            # A live root with no current terminal is not something the tombstone
            # check can adjudicate; leave it for an explicit transition.
            left_live += 1
            continue
        try:
            with SessionLocal() as db:
                confirmed_dead = is_target_confirmed_dead(terminal_id, db)
        except Exception:
            # A probe/DB hiccup is NOT death — leave the root live (D8).
            logger.debug(
                "reconcile_live_roots: deadness check failed for %s; leaving live",
                terminal_id,
                exc_info=True,
            )
            left_live += 1
            continue
        if not confirmed_dead:
            left_live += 1
            continue
        try:
            crash_detach_terminal(terminal_id)
            detached += 1
        except Exception:
            logger.exception(
                "reconcile_live_roots: crash_detach_terminal failed for %s", terminal_id
            )
            left_live += 1

    if detached or checked:
        logger.info(
            "f829_reconcile_live_roots checked=%d detached=%d left_live=%d",
            checked,
            detached,
            left_live,
        )
    return {"checked": checked, "detached": detached, "left_live": left_live}


def sweep_kiro_capture() -> dict:
    """F829 D7 driver: eager-capture poll for live kiro roots without a uuid.

    Runs one bounded poll per live kiro conversation whose provider_session_id is
    still NULL (the eager-capture window). The poller itself is idempotent and
    bounded (20 ticks / 120 s), lands ``capture_unknown`` at timeout, and
    short-circuits once captured — so a coarse periodic cadence here is safe.
    """
    from cli_agent_orchestrator.clients.database import ConversationIdentityModel as _CI
    from cli_agent_orchestrator.clients.database import (
        SessionLocal,
        get_terminal_metadata,
        list_live_conversation_roots,
    )
    from cli_agent_orchestrator.services import kiro_capture

    polled = 0
    captured = 0
    try:
        with SessionLocal() as db:
            roots = (
                db.query(_CI)
                .filter(
                    _CI.provider == "kiro_cli",
                    _CI.lifecycle == "live",
                    _CI.provider_session_id.is_(None),
                )
                .all()
            )
            targets = [(r.current_terminal_id, r.provider_namespace) for r in roots]
    except Exception:
        logger.debug("sweep_kiro_capture: could not list kiro roots", exc_info=True)
        return {"polled": 0, "captured": 0}

    for terminal_id, _ns in targets:
        if not terminal_id:
            continue
        meta = get_terminal_metadata(terminal_id)
        cwd = (meta or {}).get("working_directory")
        if not cwd:
            continue
        polled += 1
        try:
            # KAS is the v3 engine; the capture surface auto-detects, and
            # kas=True targets the v3 store the D9 probe confirmed.
            res = kiro_capture.poll_kiro_capture(terminal_id, kas=True, cwd=cwd)
            if res.get("status") == "captured":
                captured += 1
        except Exception:
            logger.debug("sweep_kiro_capture poll failed for %s", terminal_id, exc_info=True)
    return {"polled": polled, "captured": captured}
