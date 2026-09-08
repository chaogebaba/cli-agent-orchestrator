"""F829 D2: the three distinct conversation-lifecycle transitions, one
authoritative writer each.

D2 names three transitions that must never share a writer or silently overwrite
one another:

* (a) PLANNED HIBERNATE — the ``delete_terminal`` default (non-force). Requires a
  VALIDATED recoverable artifact (D6): valid → root lifecycle ``hibernated``;
  no valid artifact → a typed REFUSAL naming provider + reason (the caller may
  then choose the explicit reap). This writer NEVER lands ``hibernated`` on an
  unrecoverable conversation.
* (b) EXPLICIT REAP — ``force=True``. Proceeds regardless, reports
  unrecoverability in the result, lifecycle ``abandoned``.
* (c) CRASH-DETACH — the D8 narrow transition (``crash_detach_terminal`` in
  ``clients/database.py``), lifecycle ``detached``. Lives in the DB layer
  because it runs from the liveness pass, not a user delete.

Durable caller identity and callback correlation (``owner_principal``,
``origin_callback_ref``) are root columns and survive every transition by
construction — no writer here touches them.

This module is the pure decision+writer core. WHERE and HOW ``delete_terminal``
invokes it (and how a hibernate refusal is surfaced to the API/MCP caller) is
the wiring concern of ``terminal_service``; keeping the decision here makes each
transition independently testable and keeps ``delete_terminal``'s leased body
untouched by the lifecycle logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HibernateDecision:
    """Outcome of evaluating a PLANNED hibernate against the artifact (D6)."""

    allowed: bool
    lifecycle: Optional[str]  # 'hibernated' when allowed, else None
    provider: Optional[str] = None
    reason: Optional[str] = None  # snake_case token when refused
    identity_key: Optional[str] = None
    artifact_locator: Optional[str] = None


def _root_for_terminal(terminal_id: str):
    """Resolve the conversation root row for a terminal, or None."""
    from cli_agent_orchestrator.clients.database import (
        get_conversation_identity,
        get_terminal_identity,
    )

    ti = get_terminal_identity(terminal_id)
    if ti is None or not ti.get("identity_key"):
        return None
    return get_conversation_identity(ti["identity_key"])


def evaluate_planned_hibernate(terminal_id: str) -> HibernateDecision:
    """D2(a): decide whether a planned hibernate may proceed for ``terminal_id``.

    Resolves the conversation root, then the artifact (D6). Returns a decision;
    it does NOT write. ``allowed`` is False with a typed ``reason`` when there is
    no recoverable artifact, so the caller can refuse the destructive reap and
    let the operator choose an explicit ``force`` reap instead.

    Reason tokens (snake_case, D3 vocabulary):
    - ``session_artifact_missing``      — nothing recoverable was captured / gone.
    - ``session_artifact_unavailable``  — store inaccessible (retryable).
    - ``session_artifact_invalid``      — a file exists but does not validate.
    A terminal with no conversation root (pre-F829) is allowed to proceed
    (nothing F829 owns to protect) with ``lifecycle=None``.
    """
    from cli_agent_orchestrator.services.session_artifact import (
        ArtifactState,
        resolve_artifact,
    )

    root = _root_for_terminal(terminal_id)
    if root is None:
        # No F829 identity → no hibernate contract to enforce; let the ordinary
        # reap proceed and set no conversation lifecycle.
        return HibernateDecision(allowed=True, lifecycle=None)

    status = resolve_artifact(
        root["provider"],
        provider_session_id=root.get("provider_session_id"),
        provider_namespace=root.get("provider_namespace"),
        artifact_locator=root.get("artifact_locator"),
    )
    if status.is_valid:
        return HibernateDecision(
            allowed=True,
            lifecycle="hibernated",
            provider=root["provider"],
            identity_key=root["identity_key"],
            artifact_locator=status.locator,
        )
    reason = {
        ArtifactState.MISSING: "session_artifact_missing",
        ArtifactState.INACCESSIBLE: "session_artifact_unavailable",
        ArtifactState.INVALID: "session_artifact_invalid",
    }.get(status.state, "session_artifact_missing")
    return HibernateDecision(
        allowed=False,
        lifecycle=None,
        provider=root["provider"],
        reason=reason,
        identity_key=root["identity_key"],
    )


def commit_hibernate(decision: HibernateDecision) -> None:
    """D2(a) writer: land ``hibernated`` on the root after a successful reap.

    Only writes when the decision allowed a hibernate AND named a lifecycle
    (i.e. there was an F829 root and its artifact validated). Records a
    ``hibernated`` event with the validated artifact locator.
    """
    if not decision.allowed or decision.lifecycle is None or decision.identity_key is None:
        return
    from cli_agent_orchestrator.clients.database import (
        record_conversation_event,
        set_conversation_lifecycle,
    )

    set_conversation_lifecycle(decision.identity_key, decision.lifecycle)
    record_conversation_event(
        decision.identity_key,
        "hibernated",
        detail={"artifact_locator": decision.artifact_locator, "provider": decision.provider},
    )


def commit_reap(terminal_id: str) -> Optional[str]:
    """D2(b) writer: explicit reap (force=True) → root lifecycle ``abandoned``.

    Records an ``abandoned`` event noting unrecoverability. Returns the
    identity_key it acted on, or None if the terminal has no F829 root.
    """
    from cli_agent_orchestrator.clients.database import (
        record_conversation_event,
        set_conversation_lifecycle,
    )

    root = _root_for_terminal(terminal_id)
    if root is None:
        return None
    key = root["identity_key"]
    set_conversation_lifecycle(key, "abandoned")
    record_conversation_event(
        key,
        "abandoned",
        terminal_id=terminal_id,
        detail={"provider": root["provider"], "reason": "explicit_reap"},
    )
    return key
