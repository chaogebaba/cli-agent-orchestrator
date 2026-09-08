"""F829 A1 (D10): declared ∧ measured capability admission tests."""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services.capability_evidence import (
    CAPABILITY_KEYS,
    EvidenceState,
    admit_capability,
    provider_declares,
)


def test_d10_declared_capabilities_match_d9_verdicts():
    """The four A1 providers declare exactly the D9-recorded capabilities."""
    # codex RECOVERS on every arm.
    for op in ("fork", "resume", "capture", "artifact_locate"):
        assert provider_declares("codex", op) is True
    # kiro: resume/capture/artifact but NOT fork.
    assert provider_declares("kiro_cli", "fork") is False
    for op in ("resume", "capture", "artifact_locate"):
        assert provider_declares("kiro_cli", op) is True
    # claude: resume/capture/artifact, not fork (here).
    assert provider_declares("claude_code", "fork") is False
    assert provider_declares("claude_code", "resume") is True
    # pi: PARTIAL but declares resume/artifact (the boundary is D8, not capability).
    assert provider_declares("pi_cli", "resume") is True
    assert provider_declares("pi_cli", "artifact_locate") is True
    assert provider_declares("pi_cli", "fork") is False


def test_d10_unknown_provider_declares_nothing():
    assert provider_declares("does_not_exist", "resume") is False


def test_d10_unknown_capability_key_raises():
    with pytest.raises(ValueError):
        provider_declares("codex", "teleport")
    with pytest.raises(ValueError):
        admit_capability("codex", "teleport")


def test_d10_failed_evidence_refuses_admission():
    """Only a FAILED exact-key evidence row refuses (missing=provider_capability)."""
    v = admit_capability("codex", "resume", evidence_state=EvidenceState.FAILED)
    assert v.admitted is False
    assert v.unverified_key is None
    assert v.state is EvidenceState.FAILED


def test_d10_passed_evidence_admits_without_unverified():
    v = admit_capability("codex", "resume", evidence_state=EvidenceState.PASSED)
    assert v.admitted is True
    assert v.unverified_key is None


@pytest.mark.parametrize("state", [None, EvidenceState.UNKNOWN])
def test_d10_unmeasured_admits_but_marks_unverified(state):
    """A missing/stale/unknown key ADMITS (never refuses at runtime) but carries
    capability_unverified so the attempt is recorded as fresh evidence (D10)."""
    v = admit_capability("kiro_cli", "resume", evidence_state=state)
    assert v.admitted is True
    assert v.unverified_key == "resume"


def test_d10_capability_keys_are_the_four_axes():
    assert CAPABILITY_KEYS == frozenset({"fork", "resume", "capture", "artifact_locate"})
