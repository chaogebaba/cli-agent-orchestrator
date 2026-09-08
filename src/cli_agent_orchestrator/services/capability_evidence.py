"""F829 A1 (D10): declared ∧ measured provider capabilities.

Capabilities are DECLARED by the adapter (``BaseProvider.declared_capabilities``:
``{fork, resume, capture, artifact_locate}``) and MEASURED by the D9 probe. Two
different questions consume the two facts:

* ADVERTISING (release-time, AC1/D9): a capability is advertisable only when
  ``declaration ∧ passing evidence`` — a declared-but-unmeasured capability is
  NOT advertised. That gate lives at release time, never here.
* RUNTIME ADMISSION (this module): a resume is REFUSED only when the EXACT
  evidence key is ``failed``. A ``missing`` or ``stale`` key does NOT refuse —
  the resume is ATTEMPTED and its result carries ``capability_unverified`` with
  the unmeasured key, and the outcome is recorded as fresh evidence. Blocking on
  unmeasured capability is a release concern, never a runtime one (D10).

Evidence rows are keyed as in D9 — ``(provider, cli_version, adapter_version,
mode, store_format_fingerprint, operation)`` — with a ``state`` in
``{passed, failed, unknown}``, a timestamp, and an optional evidence path/hash.
An account change invalidates HOP evidence, not identity; ``credential_hop`` is
``unknown`` unless measured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# The capability axes an adapter declares (D10). ``credential_hop`` is a probe
# axis measured by the D10 hop probe, not a spawn-time declaration.
CAPABILITY_KEYS = frozenset({"fork", "resume", "capture", "artifact_locate"})


class EvidenceState(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CapabilityVerdict:
    """The runtime admission verdict for one (provider, operation) capability.

    * ``admitted`` — False ONLY when an exact-key evidence row is ``failed``;
      True in every other case (passed, or unmeasured).
    * ``unverified_key`` — the operation key when admission proceeds on a
      MISSING/STALE (i.e. not-``passed``, not-``failed``) evidence row, so the
      caller can surface ``capability_unverified`` (D10). None when a passing
      row was found.
    * ``state`` — the observed evidence state (or UNKNOWN when no row exists).
    """

    admitted: bool
    operation: str
    state: EvidenceState
    unverified_key: Optional[str] = None
    reason: Optional[str] = None


def provider_declares(provider: str, operation: str) -> bool:
    """Whether ``provider``'s adapter DECLARES ``operation`` (D10 matrix)."""
    from cli_agent_orchestrator.providers.manager import get_provider_class

    if operation not in CAPABILITY_KEYS:
        raise ValueError(f"unknown capability key: {operation!r}")
    try:
        cls = get_provider_class(provider)
    except ValueError:
        return False
    declared = getattr(cls, "declared_capabilities", None) or {}
    return bool(declared.get(operation, False))


def admit_capability(
    provider: str,
    operation: str,
    *,
    evidence_state: Optional[EvidenceState] = None,
) -> CapabilityVerdict:
    """The D10 RUNTIME admission decision for one capability.

    ``evidence_state`` is the observed exact-key evidence state (or None when no
    row exists / not looked up). The rule (D10):

    * ``failed``  → NOT admitted (``missing=provider_capability``, retryable).
    * ``passed``  → admitted, no ``capability_unverified``.
    * anything else (``unknown`` / None / stale) → ADMITTED, carrying
      ``capability_unverified=operation`` so the caller records the attempt as
      fresh evidence rather than refusing on an unmeasured key.

    This function does NOT read the declaration: a provider that does not declare
    the operation is refused earlier (``prepare_resume``'s capability gate); here
    we decide admission for a declared operation given its evidence.
    """
    if operation not in CAPABILITY_KEYS:
        raise ValueError(f"unknown capability key: {operation!r}")
    state = evidence_state or EvidenceState.UNKNOWN
    if state is EvidenceState.FAILED:
        return CapabilityVerdict(
            admitted=False,
            operation=operation,
            state=state,
            reason=f"{provider}_{operation}_capability_failed",
        )
    if state is EvidenceState.PASSED:
        return CapabilityVerdict(admitted=True, operation=operation, state=state)
    # missing / stale / unknown — admit, but mark unverified.
    return CapabilityVerdict(
        admitted=True,
        operation=operation,
        state=state,
        unverified_key=operation,
        reason=f"{provider}_{operation}_capability_unverified",
    )


def _load_evidence_state(
    provider: str,
    operation: str,
    *,
    cli_version: str = "*",
    adapter_version: str = "*",
    mode: str = "*",
    store_format_fingerprint: str = "*",
) -> Optional[EvidenceState]:
    """Load the persisted exact-key evidence state, or None when unmeasured.

    Thin adapter over the DB reader (kept here so the resume path imports ONE
    admission seam). A stored ``state`` string maps to ``EvidenceState``; an
    unrecognised/absent row yields None so ``admit_capability`` treats it as
    unmeasured (admit + ``capability_unverified``), never a refusal.
    """
    from cli_agent_orchestrator.clients.database import get_capability_evidence

    row = get_capability_evidence(
        provider,
        operation,
        cli_version=cli_version,
        adapter_version=adapter_version,
        mode=mode,
        store_format_fingerprint=store_format_fingerprint,
    )
    if not row:
        return None
    raw = row.get("state")
    try:
        return EvidenceState(raw)
    except ValueError:
        return None


def admit_resume_capability(
    provider: str,
    *,
    cli_version: str = "*",
    adapter_version: str = "*",
    mode: str = "*",
    store_format_fingerprint: str = "*",
) -> CapabilityVerdict:
    """D10 PRODUCTION runtime admission for a ``resume`` on ``provider``.

    The single seam the resume path (``_build_launch_spec``) calls. It:

    1. REFUSES a provider that does not DECLARE ``resume`` (``provider_declares``
       — its production caller). A non-declaring provider is never resume-capable
       (blueprint D10), so this is ``admitted=False`` before any evidence read.
    2. LOADS the persisted exact-key ``resume`` evidence and decides admission
       with ``admit_capability``: a ``failed`` row refuses; a missing/stale/
       ``unknown`` key admits carrying ``capability_unverified`` (runtime never
       blocks on an unmeasured key — that is the release-time advertising gate).

    Returns a ``CapabilityVerdict``; the caller maps ``admitted=False`` to
    ``ResumeRefused(missing="provider_capability")`` (retryable).
    """
    if not provider_declares(provider, "resume"):
        return CapabilityVerdict(
            admitted=False,
            operation="resume",
            state=EvidenceState.UNKNOWN,
            reason=f"{provider}_resume_not_declared",
        )
    state = _load_evidence_state(
        provider,
        "resume",
        cli_version=cli_version,
        adapter_version=adapter_version,
        mode=mode,
        store_format_fingerprint=store_format_fingerprint,
    )
    return admit_capability(provider, "resume", evidence_state=state)


def advertised_resumable(provider: str) -> bool:
    """D10 RELEASE-time advertising gate: declaration ∧ PASSING evidence.

    True only when the adapter DECLARES ``resume`` AND the exact-key evidence
    state is ``passed``. A declared-but-unmeasured (or failed) capability is NOT
    advertised — this is the gate that keeps kiro from being advertised
    resumable until per-attempt positive attribution is measured and passes
    (B4). Distinct from ``admit_resume_capability`` (runtime), which admits an
    unmeasured key; advertising is strictly stronger.
    """
    if not provider_declares(provider, "resume"):
        return False
    state = _load_evidence_state(provider, "resume")
    return state is EvidenceState.PASSED
