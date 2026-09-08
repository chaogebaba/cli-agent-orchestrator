"""F829 build-2 B2: the claude SessionStart transcript-binding must attach the
captured session id to the conversation_identity ROOT.

Before this wiring, ``bind_transcript`` (api/main.py) wrote only the
``transcript_bindings`` table and the truth-adapter tailer; it never called
``attach_captured_uuid`` on the F829 root, so a fresh claude worker's root kept
``provider_session_id=NULL`` and planned hibernate refused
``session_artifact_missing`` — claude could not survive an account switch by
resume (the user's stated requirement is that claude, kiro AND codex all do).
These seam tests drive the PUBLIC bind endpoint against a real DB and assert the
root's ``provider_session_id`` is set after the SessionStart hook fires, that a
re-report of the SAME id is idempotent, and that a FOREIGN id already bound to a
different identity is refused (never stolen).
"""

from __future__ import annotations

import asyncio
from pathlib import Path


def _seed_root_and_incarnation(db_mod, *, identity_key: str, terminal_id: str,
                               cwd: str, provider_namespace: str | None = None) -> None:
    """Mint a fresh claude conversation root + its live terminal_identity
    incarnation (the state right after a fresh spawn, before capture)."""
    db_mod.mint_conversation_identity(
        identity_key=identity_key,
        provider="claude_code",
        provider_namespace=provider_namespace,
        agent_profile="developer",
        model="sonnet",
        reasoning_effort=None,
        owner_principal="mb_owner",
        origin_callback_ref=None,
        current_terminal_id=terminal_id,
    )
    with db_mod.SessionLocal.begin() as s:
        s.add(
            db_mod.TerminalIdentityModel(
                terminal_id=terminal_id,
                provider="claude_code",
                agent_profile="developer",
                cwd=cwd,
                session_name="cao-test",
                provider_session_id=None,
                base_name=terminal_id,
                lifecycle="live",
                identity_key=identity_key,
            )
        )


def _projects_transcript(home: Path, session_id: str) -> Path:
    """Create ~/.claude/projects/<slug>/<session>.jsonl under a fake home."""
    proj = home / ".claude" / "projects" / "repo"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{session_id}.jsonl"
    f.write_text('{"type":"user","sessionId":"%s"}\n' % session_id, encoding="utf-8")
    return f


def _bind(terminal_id: str, session_id: str, transcript_path: str, home: Path):
    """Call the PUBLIC bind endpoint the claude SessionStart hook targets."""
    from unittest.mock import patch

    from cli_agent_orchestrator.api.main import TranscriptBindingRequest, bind_transcript

    body = TranscriptBindingRequest(
        terminal_id=terminal_id,
        session_id=session_id,
        transcript_path=transcript_path,
        source="startup",
    )
    with (
        patch("cli_agent_orchestrator.api.main.get_terminal_metadata",
              return_value={"id": terminal_id}),
        patch("cli_agent_orchestrator.api.main.provider_home") as ph,
    ):
        ph.return_value.projects = home / ".claude" / "projects"
        return asyncio.run(bind_transcript(terminal_id, body, []))


def _root_sid(db_mod, identity_key: str):
    row = db_mod.get_conversation_identity(identity_key)
    return row["provider_session_id"] if row else None


def test_sessionstart_binding_attaches_session_id_to_root(real_sqlite_env, tmp_path):
    """After the claude SessionStart hook binds, the F829 root's
    provider_session_id equals the hook-reported session id (so the worker is
    hibernate-eligible / resumable across an account switch)."""
    import cli_agent_orchestrator.clients.database as db_mod

    home = tmp_path / "home"
    sid = "claude-sess-0001"
    _seed_root_and_incarnation(db_mod, identity_key="conv_ccc10001",
                               terminal_id="ccc10001", cwd="/work")
    assert _root_sid(db_mod, "conv_ccc10001") is None  # NULL before capture

    tpath = _projects_transcript(home, sid)
    result = _bind("ccc10001", sid, str(tpath), home)
    assert result["success"] is True

    # THE POINT: the root now carries the captured id.
    assert _root_sid(db_mod, "conv_ccc10001") == sid


def test_re_report_of_same_id_is_idempotent(real_sqlite_env, tmp_path):
    """A second SessionStart for the SAME session id leaves the root bound to
    that id (idempotent), not an error."""
    import cli_agent_orchestrator.clients.database as db_mod

    home = tmp_path / "home"
    sid = "claude-sess-0002"
    _seed_root_and_incarnation(db_mod, identity_key="conv_ccc20001",
                               terminal_id="ccc20001", cwd="/work")
    tpath = _projects_transcript(home, sid)
    assert _bind("ccc20001", sid, str(tpath), home)["success"] is True
    assert _bind("ccc20001", sid, str(tpath), home)["success"] is True
    assert _root_sid(db_mod, "conv_ccc20001") == sid


def test_foreign_id_already_bound_elsewhere_is_refused_not_stolen(real_sqlite_env, tmp_path):
    """If the reported id is already bound to ANOTHER identity, the bind endpoint
    still returns 200 (the transcript binding is recorded) but the root is NOT
    re-pointed to the foreign id — attach_captured_uuid refuses the steal."""
    import cli_agent_orchestrator.clients.database as db_mod

    home = tmp_path / "home"
    foreign_sid = "claude-sess-FOREIGN"

    # An OTHER identity already owns foreign_sid.
    _seed_root_and_incarnation(db_mod, identity_key="conv_other", terminal_id="other001",
                               cwd="/work")
    assert db_mod.bind_provider_session_id("conv_other", provider_session_id=foreign_sid)

    # Our root is fresh (NULL). A (buggy/copied) SessionStart reports the foreign id.
    _seed_root_and_incarnation(db_mod, identity_key="conv_mine", terminal_id="mine001",
                               cwd="/work")
    tpath = _projects_transcript(home, foreign_sid)
    result = _bind("mine001", foreign_sid, str(tpath), home)
    # The transcript binding itself is fine; the ROOT is protected.
    assert result["success"] is True
    assert _root_sid(db_mod, "conv_mine") is None  # NOT stolen
    assert _root_sid(db_mod, "conv_other") == foreign_sid  # still the owner's
