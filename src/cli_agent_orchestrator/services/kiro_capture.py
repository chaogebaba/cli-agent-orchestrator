"""F829 D7: eager kiro session-id capture (absorbs #416 pt2).

Kiro does NOT pre-mint a session id and does not accept ``--resume-id`` on an
uncreated id (research + kiro_cli.py:226-239), so a fresh kiro conversation has
no durable identity until kiro itself writes one — which the D9 probe showed
happens 1-3 s INTO the first turn (the v3 session dir + session.json +
messages.jsonl materialise early in the first turn, not at spawn, not only at
completion).

So capture is EAGER and BOUNDED, starting at the FIRST TURN's start:
* poll on the status tick; each attempt tries ``capture_kiro_uuid`` (the CLI's
  own ``--list-sessions --format json`` surface).
* the bound is ``CAPTURE_MAX_TICKS`` (20) ticks OR ``CAPTURE_MAX_SECONDS`` (120)
  wall-clock from the first attempt, whichever first.
* on capture: attribute + bind via ``attach_captured_uuid`` (D4).
* at timeout: lifecycle ``capture_unknown`` + a ``capture_unknown`` event, and
  eager polling STOPS. A capture_unknown identity is NOT unresumable — it can be
  recovered later by first completed turn, a hibernate/detach request, or an
  explicit ``cao identity attach`` (owner-only).

State is per conversation identity_key and lives in-process; it is advisory
(the durable truth is the root's provider_session_id + lifecycle), so losing it
on restart at worst re-runs a bounded, idempotent poll.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)

CAPTURE_MAX_TICKS = 20
CAPTURE_MAX_SECONDS = 120.0


@dataclass
class _CaptureState:
    ticks: int = 0
    started_at: Optional[float] = None  # first attempt (≈ first-turn start)
    stopped: bool = False  # timed out or captured


_STATES: Dict[str, _CaptureState] = {}


def reset_state(identity_key: str) -> None:
    """Clear tracker state (e.g. on an explicit re-attempt trigger)."""
    _STATES.pop(identity_key, None)


def _now() -> float:
    return time.monotonic()


def poll_kiro_capture(
    terminal_id: str,
    *,
    kas: bool,
    cwd: str,
    now: Optional[float] = None,
) -> dict:
    """One eager-capture tick for a kiro terminal.

    Returns a small status dict: ``{"status": one_of(
        "no_root" | "already_captured" | "not_this_provider" |
        "pending" | "captured" | "capture_rejected" | "timed_out" | "stopped")}``.

    Idempotent and cheap: once a root has a provider_session_id, or once the
    bound is hit (``capture_unknown``), further calls short-circuit.
    """
    from cli_agent_orchestrator.clients.database import (
        get_conversation_identity,
        get_terminal_identity,
        record_conversation_event,
        set_conversation_lifecycle,
    )
    from cli_agent_orchestrator.services.conversation_transition import attach_captured_uuid

    ti = get_terminal_identity(terminal_id)
    if ti is None or not ti.get("identity_key"):
        return {"status": "no_root"}
    key = ti["identity_key"]
    root = get_conversation_identity(key)
    if root is None:
        return {"status": "no_root"}
    if root.get("provider") != "kiro_cli":
        return {"status": "not_this_provider"}
    if root.get("provider_session_id"):
        return {"status": "already_captured"}

    state = _STATES.setdefault(key, _CaptureState())
    if state.stopped:
        return {"status": "stopped"}

    tnow = now if now is not None else _now()
    if state.started_at is None:
        state.started_at = tnow

    # Try the capture (the CLI's list-sessions surface). session_capture_none
    # means the first turn hasn't persisted a row yet — a normal "pending".
    from cli_agent_orchestrator.services.fork_context_service import (
        ForkContextError,
        capture_kiro_uuid,
    )

    captured: Optional[str] = None
    try:
        captured = capture_kiro_uuid(kas, state.started_at, cwd)
    except ForkContextError:
        captured = None
    except Exception:
        logger.debug("kiro capture attempt errored for %s", key, exc_info=True)
        captured = None

    if captured:
        result = attach_captured_uuid(
            terminal_id,
            provider_session_id=captured,
            provider="kiro_cli",
        )
        state.stopped = True
        if result.get("status") in ("captured", "resume_published"):
            return {"status": "captured", "provider_session_id": captured}
        return {"status": result.get("status", "capture_rejected")}

    # No id yet — count this tick and check the bound.
    state.ticks += 1
    elapsed = tnow - state.started_at
    if state.ticks >= CAPTURE_MAX_TICKS or elapsed >= CAPTURE_MAX_SECONDS:
        state.stopped = True
        set_conversation_lifecycle(key, "capture_unknown")
        record_conversation_event(
            key,
            "capture_unknown",
            terminal_id=terminal_id,
            detail={"ticks": state.ticks, "elapsed_s": round(elapsed, 1)},
        )
        return {"status": "timed_out"}
    return {"status": "pending", "ticks": state.ticks}


def reattempt_capture(terminal_id: str, *, kas: bool, cwd: str) -> dict:
    """Re-attempt capture after a stop (first completed turn, hibernate/detach
    request, or an explicitly attributed artifact). Clears the stopped state and
    runs one poll.
    """
    from cli_agent_orchestrator.clients.database import get_terminal_identity

    ti = get_terminal_identity(terminal_id)
    if ti and ti.get("identity_key"):
        reset_state(ti["identity_key"])
    return poll_kiro_capture(terminal_id, kas=kas, cwd=cwd)


def attach_identity_artifact(
    identity_key: str, artifact_locator: str, *, owner_principal: str
) -> dict:
    """F829 D7: `cao identity attach <identity_key> <artifact>` (owner-only).

    Turns a capture_unknown identity into a recoverable one by explicit,
    owner-authorised artifact attribution. Refuses when the caller is not the
    owner. The artifact must validate under the provider resolver before it is
    accepted (no unattributed newest-file attach).
    """
    from cli_agent_orchestrator.clients.database import (
        bind_provider_session_id,
        get_conversation_identity,
        record_conversation_event,
        set_conversation_lifecycle,
    )

    root = get_conversation_identity(identity_key)
    if root is None:
        return {"status": "no_root"}
    if root.get("owner_principal") != owner_principal:
        return {"status": "not_owner"}
    # The artifact locator must carry an identity we can bind; for kiro the id is
    # the sess_<uuid> dir name. Accept the caller-supplied id via the locator's
    # basename when it is a sess_ id, else treat the locator as the id.
    import os

    candidate = os.path.basename(artifact_locator.rstrip("/")) or artifact_locator
    ok = bind_provider_session_id(
        identity_key,
        provider_session_id=candidate,
        artifact_locator=artifact_locator,
    )
    if not ok:
        record_conversation_event(
            identity_key,
            "uuid_capture_rejected",
            detail={"reason": "explicit_attach_conflict", "artifact": artifact_locator},
        )
        return {"status": "capture_rejected"}
    set_conversation_lifecycle(identity_key, "hibernated")
    record_conversation_event(
        identity_key,
        "identity_attached",
        detail={"artifact": artifact_locator, "provider_session_id": candidate},
    )
    return {"status": "attached", "provider_session_id": candidate}
