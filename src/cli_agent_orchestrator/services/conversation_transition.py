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


# ---------------------------------------------------------------------------
# F829 D3: resume authorize -> classify -> claim (the pre-spawn gate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResumeAdmission:
    """Outcome of the D3 pre-spawn gate (authorize + classify + claim)."""

    ok: bool
    identity_key: str
    error: Optional[str] = None  # snake_case token when refused
    generation: Optional[int] = None  # the generation the claim was taken at
    provider: Optional[str] = None
    provider_session_id: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    provider_namespace: Optional[str] = None


def authorize_and_classify_resume(root: dict, caller_principal: Optional[str]) -> "ResumeAdmission":
    """D3 steps 2-3 (authorize + classify), NO claim yet.

    * AUTHORIZE against ``owner_principal``:
      - owner set and != caller → ``resume_not_owner``.
      - owner NULL (a top-level spawn root, or a legacy_unknown_owner root) →
        ``resume_not_owner`` too: it is claimable only by an explicit
        ``cao identity claim`` (supervisor ask 1), never silently by a
        requester.
    * CLASSIFY the lifecycle:
      - resumable set {hibernated, detached} → ok.
      - abandoned → session_abandoned; live → session_live_owned; expired →
        session_expired; capture_unknown → session_artifact_missing.
    """
    from cli_agent_orchestrator.services.fork_context_service import (
        F829_CLASSIFY_TOKENS,
        F829_RESUMABLE_LIFECYCLES,
    )

    key = root["identity_key"]
    owner = root.get("owner_principal")
    # AUTHORIZE. A NULL owner is NOT open season — it requires an explicit claim.
    if owner is None or (caller_principal is not None and owner != caller_principal):
        return ResumeAdmission(ok=False, identity_key=key, error="resume_not_owner")

    # CLASSIFY.
    lifecycle = root.get("lifecycle")
    if lifecycle not in F829_RESUMABLE_LIFECYCLES:
        token = F829_CLASSIFY_TOKENS.get(lifecycle or "", "session_artifact_missing")
        return ResumeAdmission(ok=False, identity_key=key, error=token)

    return ResumeAdmission(
        ok=True,
        identity_key=key,
        generation=int(root.get("generation", 0)),
        provider=root.get("provider"),
        provider_session_id=root.get("provider_session_id"),
        model=root.get("model"),
        reasoning_effort=root.get("reasoning_effort"),
        provider_namespace=root.get("provider_namespace"),
    )


def claim_resume_admission(admission: "ResumeAdmission", claimant: str) -> "ResumeAdmission":
    """D3 step 4: take the CAS claim for an already-authorized+classified resume.

    Returns the admission unchanged on success; on a lost CAS (another claimant
    or a moved generation) returns a refused admission with
    ``session_resume_in_progress``. The caller must NOT spawn on a refusal.
    """
    from cli_agent_orchestrator.clients.database import claim_resume, record_conversation_event

    if not admission.ok or admission.generation is None:
        return admission
    won = claim_resume(admission.identity_key, admission.generation, claimant)
    if not won:
        return ResumeAdmission(
            ok=False, identity_key=admission.identity_key, error="session_resume_in_progress"
        )
    record_conversation_event(
        admission.identity_key,
        "resume_claimed",
        detail={"claimant": claimant, "generation": admission.generation + 1},
    )
    return admission


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    identity_key: str
    error: Optional[str] = None
    published_session_id: Optional[str] = None


def _same_file(path_a: Optional[str], path_b: Optional[str]) -> bool:
    """inode/realpath equality for the claude divergence branch."""
    if not path_a or not path_b:
        return False
    import os

    try:
        return os.path.realpath(path_a) == os.path.realpath(path_b) or os.path.samefile(
            path_a, path_b
        )
    except OSError:
        return False


def verify_and_publish_resume(
    admission: "ResumeAdmission",
    *,
    terminal_id: str,
    reported_session_id: Optional[str],
    reported_artifact_locator: Optional[str] = None,
    provider: Optional[str] = None,
) -> "VerifyResult":
    """D3 steps 6-7: verify resumed identity + readiness, then publish.

    The provider-reported / hook-resolved session id MUST equal the root's
    ``provider_session_id`` — EXCEPT claude_code, where a documented divergence
    exists: if the hook-resolved id differs, the resume is accepted only when the
    hook-resolved transcript path is the SAME FILE as the stored artifact_locator
    (inode/realpath equality), and the root's ``provider_session_id`` is then
    rebound to the hook-resolved id inside the publish (UNIQUE re-checked; a
    conflict → ``session_identity_conflict``, claim cleared, NO publish).

    Publishing moves ``current_terminal_id`` and clears the claim — only AFTER
    verification (never before; AC7 mutant). Any failure clears the claim, keeps
    the identity retryable, and never publishes.
    """
    from cli_agent_orchestrator.clients.database import (
        bind_provider_session_id,
        clear_resume_claim,
        publish_current_terminal,
        record_conversation_event,
    )

    key = admission.identity_key
    prov = provider or admission.provider
    expected = admission.provider_session_id

    def _fail(token: str) -> "VerifyResult":
        clear_resume_claim(key, event="resume_failed")
        record_conversation_event(
            key, "resume_failed", terminal_id=terminal_id, detail={"error": token}
        )
        return VerifyResult(ok=False, identity_key=key, error=token)

    # Identity match. A None expected means the root had no captured uuid yet;
    # accept the reported id as the binding (capture-on-resume for a
    # capture_unknown root that became addressable by identity_key).
    publish_uuid = expected
    if expected is not None and reported_session_id != expected:
        if prov == "claude_code":
            # Divergence branch: accept iff the hook-resolved transcript path is
            # the SAME FILE as the stored artifact locator (inode/realpath).
            stored = _stored_artifact(key)
            if _same_file(reported_artifact_locator, stored):
                if not bind_provider_session_id(
                    key,
                    provider_session_id=reported_session_id or expected,
                    artifact_locator=reported_artifact_locator,
                ):
                    record_conversation_event(
                        key,
                        "resume_failed",
                        terminal_id=terminal_id,
                        detail={"error": "session_identity_conflict"},
                    )
                    clear_resume_claim(key)
                    return VerifyResult(
                        ok=False, identity_key=key, error="session_identity_conflict"
                    )
                publish_uuid = reported_session_id
                record_conversation_event(
                    key,
                    "claude_divergence_rebound",
                    terminal_id=terminal_id,
                    detail={"from": expected, "to": reported_session_id},
                )
            else:
                return _fail("session_identity_mismatch")
        else:
            return _fail("session_identity_mismatch")
    elif expected is None and reported_session_id:
        publish_uuid = reported_session_id

    publish_current_terminal(
        key,
        terminal_id=terminal_id,
        provider_session_id=publish_uuid,
        artifact_locator=reported_artifact_locator,
        lifecycle="live",
    )
    record_conversation_event(
        key,
        "resume_published",
        terminal_id=terminal_id,
        detail={"provider_session_id": publish_uuid},
    )
    return VerifyResult(ok=True, identity_key=key, published_session_id=publish_uuid)


def _stored_artifact(identity_key: str) -> Optional[str]:
    from cli_agent_orchestrator.clients.database import get_conversation_identity

    root = get_conversation_identity(identity_key)
    return root.get("artifact_locator") if root else None
