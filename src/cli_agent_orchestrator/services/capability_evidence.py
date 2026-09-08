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
