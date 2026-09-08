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


# --------------------------------------------------------------------------
# D10 PRODUCTION seam (verdict B3): evidence is persisted, read by the resume
# path (_build_launch_spec via prepare_resume), and a FAILED row refuses through
# the same path AC1/AC2 use. These are NOT pure-helper tests.
# --------------------------------------------------------------------------

from cli_agent_orchestrator.clients import database as d  # noqa: E402
from cli_agent_orchestrator.services.capability_evidence import (  # noqa: E402
    admit_resume_capability,
    advertised_resumable,
)
from cli_agent_orchestrator.services.resume_service import (  # noqa: E402
    ResumeRefused,
    prepare_resume,
)


def _mkresumable(key, provider, *, owner="mb_a", uuid="u-1", lifecycle="hibernated"):
    d.mint_conversation_identity(
        identity_key=key,
        provider=provider,
        provider_namespace="ns",
        agent_profile="dev",
        model="m1",
        reasoning_effort=None,
        owner_principal=owner,
        origin_callback_ref=None,
        current_terminal_id=f"t_{key}",
    )
    d.bind_provider_session_id(key, provider_session_id=uuid, provider_namespace="ns")
    d.set_conversation_lifecycle(key, lifecycle)
    d.upsert_recovery_manifest(key, cwd="/tmp")


def test_d10_evidence_roundtrip_exact_key(real_sqlite_env):
    """The persisted store reads back the exact-key row it wrote."""
    assert d.get_capability_evidence("codex", "resume") is None  # unmeasured
    d.record_capability_evidence("codex", "resume", "passed")
    row = d.get_capability_evidence("codex", "resume")
    assert row is not None and row["state"] == "passed"
    # a later measurement of the SAME key overwrites (newest is truth).
    d.record_capability_evidence("codex", "resume", "failed")
    assert d.get_capability_evidence("codex", "resume")["state"] == "failed"


def test_d10_failed_evidence_refuses_through_prepare_resume(real_sqlite_env):
    """B3: a FAILED exact-key row makes the PRODUCTION resume path refuse with
    missing=provider_capability — not a pure-helper call with an injected arg."""
    _mkresumable("kf", "codex", owner="mb_a", uuid="cf-uuid")
    d.record_capability_evidence("codex", "resume", "failed")
    with pytest.raises(ResumeRefused) as ei:
        prepare_resume(
            resume_from="kf",
            requested_agent_profile=None,
            requested_working_directory="/tmp",
            caller_principal="mb_a",
        )
    assert ei.value.missing == "provider_capability"
    assert ei.value.identity_key == "kf"
    assert ei.value.retryable is True
    assert ei.value.reason == "codex_resume_capability_failed"


def test_d10_unmeasured_admits_and_marks_unverified_through_prepare_resume(real_sqlite_env):
    """An UNMEASURED exact key admits (runtime never blocks on unmeasured) and
    the built spec carries capability_unverified=resume (recorded as fresh
    evidence downstream)."""
    _mkresumable("ku", "codex", owner="mb_a", uuid="cu-uuid")
    assert d.get_capability_evidence("codex", "resume") is None
    out = prepare_resume(
        resume_from="ku",
        requested_agent_profile=None,
        requested_working_directory="/tmp",
        caller_principal="mb_a",
    )
    spec = out["launch_spec"]
    assert spec.capability_unverified == "resume"
    assert spec.fork_context.capability_unverified == "resume"


def test_d10_passed_evidence_admits_without_unverified_through_prepare_resume(real_sqlite_env):
    _mkresumable("kp", "codex", owner="mb_a", uuid="cp-uuid")
    d.record_capability_evidence("codex", "resume", "passed")
    out = prepare_resume(
        resume_from="kp",
        requested_agent_profile=None,
        requested_working_directory="/tmp",
        caller_principal="mb_a",
    )
    spec = out["launch_spec"]
    assert spec.capability_unverified is None


def test_d10_admit_resume_refuses_non_declaring_provider(real_sqlite_env):
    """provider_declares gets its PRODUCTION caller: a provider that does not
    declare resume is refused before any evidence read (declared∧measured)."""
    v = admit_resume_capability("does_not_exist")
    assert v.admitted is False
    assert v.reason == "does_not_exist_resume_not_declared"


def test_d10_advertised_gate_needs_declaration_and_passing(real_sqlite_env):
    """Release-time advertising = declaration ∧ PASSING evidence. A declared but
    UNMEASURED capability is NOT advertised (the guard B4 relies on)."""
    # codex declares resume; unmeasured → NOT advertised yet.
    assert advertised_resumable("codex") is False
    d.record_capability_evidence("codex", "resume", "passed")
    assert advertised_resumable("codex") is True
    # a failed measurement un-advertises.
    d.record_capability_evidence("codex", "resume", "failed")
    assert advertised_resumable("codex") is False
    # a provider that does not declare resume is never advertised.
    assert advertised_resumable("does_not_exist") is False
