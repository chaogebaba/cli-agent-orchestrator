"""F829 A1 (D3): prepare_resume via the identity root + recovery_manifest —
ResumeLaunchSpec per-provider arms and the six-category resume_refused envelope.

These are UNIT arms over the real-sqlite fixture (no live provider); the
integrated live arms are AC1 (separate, box/e2e).
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.clients import database as d
from cli_agent_orchestrator.services.resume_service import ResumeRefused, prepare_resume


def _mkresumable(
    key,
    provider,
    *,
    owner="mb_owner",
    uuid="u-1",
    lifecycle="hibernated",
    ns="ns",
    model="m1",
    artifact=None,
    cwd="/tmp",
):
    """Mint a resumable root (owner set, uuid captured, hibernated/detached) with
    a recovery manifest carrying cwd + optional artifact locator."""
    d.mint_conversation_identity(
        identity_key=key,
        provider=provider,
        provider_namespace=ns,
        agent_profile="dev",
        model=model,
        reasoning_effort=None,
        owner_principal=owner,
        origin_callback_ref=None,
        current_terminal_id=f"t_{key}",
    )
    d.bind_provider_session_id(key, provider_session_id=uuid, provider_namespace=ns)
    if artifact:
        # bind_provider_session_id set the uuid; set the artifact locator too.
        d.publish_current_terminal(
            key, terminal_id=f"t_{key}", provider_session_id=uuid, artifact_locator=artifact
        )
    d.set_conversation_lifecycle(key, lifecycle)
    d.upsert_recovery_manifest(key, cwd=cwd)


def test_d3_codex_arm_builds_resume_fork_context(real_sqlite_env):
    _mkresumable("kx", "codex", owner="mb_a", uuid="cx-uuid")
    out = prepare_resume(
        resume_from="kx",
        requested_agent_profile=None,
        requested_working_directory="/tmp",
        caller_principal="mb_a",
    )
    assert out["via_identity"] is True
    spec = out["launch_spec"]
    assert spec.provider == "codex" and spec.provider_session_id == "cx-uuid"
    assert spec.fork_context is not None and spec.fork_context.mode == "resume"
    assert spec.fork_context.session_uuid == "cx-uuid"
    assert spec.resume_session_id is None and spec.session_artifact_path is None
    # model inherited from the root (M6 re-assert happens downstream).
    assert spec.model == "m1"


def test_d3_claude_arm_threads_resume_session_id(real_sqlite_env):
    _mkresumable("kc", "claude_code", owner="mb_a", uuid="cc-uuid")
    out = prepare_resume(
        resume_from="kc",
        requested_agent_profile=None,
        requested_working_directory="/tmp",
        caller_principal="mb_a",
    )
    spec = out["launch_spec"]
    assert spec.provider == "claude_code"
    assert spec.resume_session_id == "cc-uuid"
    # carried on the resume-mode fork_context too, so the single thread reaches claude.
    assert spec.fork_context.resume_session_id == "cc-uuid"


def test_d3_pi_arm_uses_session_artifact_path(real_sqlite_env):
    _mkresumable(
        "kp", "pi_cli", owner="mb_a", uuid="pi-uuid", artifact="/h/.pi/sessions/x/ts_pi-uuid.jsonl"
    )
    out = prepare_resume(
        resume_from="kp",
        requested_agent_profile=None,
        requested_working_directory="/tmp",
        caller_principal="mb_a",
    )
    spec = out["launch_spec"]
    assert spec.provider == "pi_cli"
    assert spec.session_artifact_path == "/h/.pi/sessions/x/ts_pi-uuid.jsonl"
    assert spec.fork_context.session_artifact_path == "/h/.pi/sessions/x/ts_pi-uuid.jsonl"
    assert spec.resume_session_id is None


def test_d3_pi_without_artifact_refuses_missing_artifact(real_sqlite_env):
    # pi root with a uuid but NO recorded artifact locator → missing=artifact.
    _mkresumable("kp2", "pi_cli", owner="mb_a", uuid="pi-uuid-2", artifact=None)
    with pytest.raises(ResumeRefused) as ei:
        prepare_resume(
            resume_from="kp2",
            requested_agent_profile=None,
            requested_working_directory="/tmp",
            caller_principal="mb_a",
        )
    assert ei.value.missing == "artifact"
    assert ei.value.identity_key == "kp2"


def test_d3_not_owner_refuses_identity_without_key(real_sqlite_env):
    _mkresumable("ko", "codex", owner="mb_owner", uuid="o-uuid")
    with pytest.raises(ResumeRefused) as ei:
        prepare_resume(
            resume_from="ko",
            requested_agent_profile=None,
            requested_working_directory="/tmp",
            caller_principal="mb_someone_else",
        )
    assert ei.value.missing == "identity"
    assert ei.value.reason == "resume_not_owner"
    # A2.3: an UNAUTHORIZED (ownership) refusal leaks NO foreign identity_key.
    assert ei.value.identity_key is None
    env = ei.value.as_dict()
    assert env["error"] == "resume_refused" and env["missing"] == "identity"
    assert "identity_key" not in env or env["identity_key"] is None


def test_d3_live_owned_refuses_identity(real_sqlite_env):
    _mkresumable("kl", "codex", owner="mb_a", uuid="l-uuid", lifecycle="live")
    with pytest.raises(ResumeRefused) as ei:
        prepare_resume(
            resume_from="kl",
            requested_agent_profile=None,
            requested_working_directory="/tmp",
            caller_principal="mb_a",
        )
    assert ei.value.missing == "identity"
    assert ei.value.reason == "session_live_owned"


def test_d3_abandoned_refuses_identity(real_sqlite_env):
    _mkresumable("ka", "codex", owner="mb_a", uuid="a-uuid", lifecycle="abandoned")
    with pytest.raises(ResumeRefused) as ei:
        prepare_resume(
            resume_from="ka",
            requested_agent_profile=None,
            requested_working_directory="/tmp",
            caller_principal="mb_a",
        )
    assert ei.value.reason == "session_abandoned" and ei.value.missing == "identity"


def test_d3_envelope_has_exactly_six_missing_tokens():
    from cli_agent_orchestrator.services.resume_service import RESUME_MISSING_TOKENS

    assert RESUME_MISSING_TOKENS == frozenset(
        {"identity", "session_id", "artifact", "cwd", "provider_capability", "profile"}
    )
