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
from typing import Any, Dict, Optional, cast

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HibernateDecision:
    """Outcome of evaluating a PLANNED hibernate against the artifact (D6)."""

    allowed: bool
    lifecycle: Optional[str]  # 'hibernated' when allowed, else None
    provider: Optional[str] = None
    reason: Optional[str] = None  # snake_case token when refused
    detail: Optional[str] = None  # the resolver's human detail, carried to the caller
    identity_key: Optional[str] = None
    artifact_locator: Optional[str] = None


def _root_for_terminal(terminal_id: str) -> Optional[Dict[str, Any]]:
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
    - ``capture_unknown``               — the root never captured an id (kiro/codex
                                          pre-capture); refuses under the same shape.
    - ``session_artifact_missing``      — nothing recoverable was captured / gone.
    - ``session_artifact_unavailable``  — store inaccessible (retryable).
    - ``session_artifact_invalid``      — a file exists but does not validate.
    A terminal with no conversation root (pre-F829) is allowed to proceed
    (nothing F829 owns to protect) with ``lifecycle=None``.

    Cheap by construction (supervisor guard 1): a single filesystem artifact
    resolve, NO provider round-trip, so a bulk reap of many terminals is not
    slowed.
    """
    from cli_agent_orchestrator.services.session_artifact import (
        ArtifactState,
        resolve_artifact,
    )

    root = _root_for_terminal(terminal_id)
    if root is None:
        # A2.4 (astra Q4): a terminal with no canonical root — or a dangling
        # link — has no honest recovery promise. Refuse the PLANNED hibernate
        # INTACT (resumable:false) with a typed ``identity_missing`` reason,
        # BEFORE any destructive cleanup, so the caller may choose an explicit
        # ``force`` reap (which proceeds with the same diagnosis and no recovery
        # promise). Post-A2 every fresh spawn mints a root, so this is the
        # missing/dangling-identity case, not the normal path.
        return HibernateDecision(
            allowed=False,
            lifecycle=None,
            reason="identity_missing",
            detail="no canonical conversation root/link for this terminal",
        )

    # A capture_unknown root (kiro/codex pre-capture) has no recoverable identity
    # yet — refuse under the same shape with the capture_unknown reason, without
    # even resolving an artifact (there is no id to resolve).
    if root.get("lifecycle") == "capture_unknown":
        return HibernateDecision(
            allowed=False,
            lifecycle=None,
            provider=root["provider"],
            reason="capture_unknown",
            detail="conversation has no captured session id yet",
            identity_key=root["identity_key"],
        )

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
        detail=status.detail,
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
    return cast(str, key)


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


def authorize_and_classify_resume(
    root: Dict[str, Any], caller_principal: Optional[str]
) -> "ResumeAdmission":
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
    # AUTHORIZE (A2.2, bypass 1 closed). Refuse when the caller is MISSING, the
    # owner is MISSING, or they differ. Previously a ``None`` caller_principal
    # silently passed any non-NULL owner (`caller_principal is not None and …`)
    # — a request that could not identify itself was authorized. A NULL owner is
    # also NOT open season: it requires an explicit `cao identity claim`.
    if caller_principal is None or owner is None or owner != caller_principal:
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
    # F829 AC5: record the RECOVERING supervisor separately as resumed_by — it is
    # NOT the durable owner (owner_principal on the root is preserved untouched),
    # so a bare callback still routes to the ORIGINAL caller, never to whoever
    # requested the recovery.
    record_conversation_event(
        admission.identity_key,
        "resumed_by",
        detail={"resumed_by": claimant},
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
    # D10 [A1-r8]: a successful resume MEASURES the resume capability — record it
    # as fresh PASSING evidence so a previously unmeasured (capability_unverified)
    # key is now measured. Best-effort; never fail a publish over evidence.
    if prov:
        try:
            from cli_agent_orchestrator.clients.database import record_capability_evidence

            record_capability_evidence(str(prov), "resume", "passed")
        except Exception:
            logger.debug("resume evidence record (passed) failed for %s", key, exc_info=True)
    return VerifyResult(ok=True, identity_key=key, published_session_id=publish_uuid)


def _stored_artifact(identity_key: str) -> Optional[str]:
    from cli_agent_orchestrator.clients.database import get_conversation_identity

    root = get_conversation_identity(identity_key)
    return cast(Optional[str], root.get("artifact_locator")) if root else None


# ---------------------------------------------------------------------------
# F829 D4: capture attach (positive attribution + not-owned-by-another) — and
# the convergence point where a RESUME completes verify+publish.
# ---------------------------------------------------------------------------


def attach_captured_uuid(
    terminal_id: str,
    *,
    provider_session_id: str,
    provider: Optional[str] = None,
    provider_namespace: Optional[str] = None,
    artifact_locator: Optional[str] = None,
) -> Dict[str, Any]:
    """F829 D4: bind a captured provider uuid to this terminal's conversation root.

    Two cases converge here (this is the point where the reported id first
    exists):

    * RESUME completion (D3 steps 6-7): if the root holds an active resume claim,
      run verify_and_publish_resume — the reported id must match (or the claude
      divergence branch applies), then publish current_terminal_id + clear claim.
    * FRESH capture (D4): otherwise bind the uuid iff it is not already bound to
      ANOTHER identity (bind_provider_session_id enforces bound-uniqueness).
      A conflict records a ``uuid_capture_rejected`` event naming both identities
      and leaves the root unbound (never a newest-file / cwd-match attach).

    Best-effort and non-raising: a terminal with no F829 root (pre-F829) returns
    ``{"status": "no_root"}`` and the caller proceeds unchanged.
    """
    from cli_agent_orchestrator.clients.database import (
        bind_provider_session_id,
        get_conversation_identity,
        record_conversation_event,
        set_conversation_lifecycle,
    )

    root = _root_for_terminal(terminal_id)
    if root is None:
        return {"status": "no_root"}
    key = root["identity_key"]

    # RESUME completion path: an active claim means this capture is the resumed
    # worker reporting its id; verify + publish rather than a fresh bind.
    if root.get("resume_claim"):
        admission = ResumeAdmission(
            ok=True,
            identity_key=key,
            generation=int(root.get("generation", 0)),
            provider=provider or root.get("provider"),
            provider_session_id=root.get("provider_session_id"),
            provider_namespace=provider_namespace or root.get("provider_namespace"),
        )
        vr = verify_and_publish_resume(
            admission,
            terminal_id=terminal_id,
            reported_session_id=provider_session_id,
            reported_artifact_locator=artifact_locator,
            provider=provider or root.get("provider"),
        )
        return {"status": "resume_published" if vr.ok else "resume_failed", "error": vr.error}

    # FRESH capture path: bind iff not owned by another identity.
    ok = bind_provider_session_id(
        key,
        provider_session_id=provider_session_id,
        provider_namespace=provider_namespace,
        artifact_locator=artifact_locator,
    )
    if not ok:
        # Find the conflicting identity for the rejection event.
        conflicting = None
        from cli_agent_orchestrator.clients.database import ConversationIdentityModel as _CI
        from cli_agent_orchestrator.clients.database import (
            SessionLocal,
        )

        try:
            with SessionLocal() as db:
                ns = (
                    provider_namespace
                    if provider_namespace is not None
                    else root.get("provider_namespace")
                )
                row = (
                    db.query(_CI)
                    .filter(
                        _CI.provider == root["provider"],
                        _CI.provider_namespace == ns,
                        _CI.provider_session_id == provider_session_id,
                        _CI.identity_key != key,
                    )
                    .first()
                )
                conflicting = row.identity_key if row is not None else None
        except Exception:
            conflicting = None
        record_conversation_event(
            key,
            "uuid_capture_rejected",
            terminal_id=terminal_id,
            detail={
                "provider_session_id": provider_session_id,
                "conflicting_identity": conflicting,
                "reason": "already_bound_to_another_identity",
            },
        )
        return {"status": "capture_rejected", "conflicting_identity": conflicting}

    record_conversation_event(
        key,
        "uuid_captured",
        terminal_id=terminal_id,
        detail={"provider_session_id": provider_session_id},
    )
    # A capture_unknown root that just captured its id becomes recoverable.
    updated = get_conversation_identity(key)
    if updated and updated.get("lifecycle") == "capture_unknown":
        set_conversation_lifecycle(key, "live")
    return {"status": "captured"}
