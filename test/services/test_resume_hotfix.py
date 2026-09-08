"""RESUME HOT-FIX slice — unit tests for the deployed resume verb.

Covers the four deliverables of ``/data/cao-scratch/briefs/resume-hotfix-dev.md``:

1. ``resolve_resume_target`` — live id, reaped id, bare uuid (identity registry
   then provider_sessions fallback), unknown → resume_refused.identity,
   no-session-id → resume_refused.session_id.
2. kiro resume capability (``supports_resume`` True while ``supports_fork_context``
   False) + the ``--resume-id`` prefix rule (id is verbatim ``sess_<uuid>``).
3. reap-time kiro capture from a FIXTURE session store, and the reap
   resume_key/resume_hint return; worktree kept when the branch has unmerged
   commits.
4. the ONE typed refusal shape; inherit_pins copies the reaped pin set.

Every assertion runs against the real SQLAlchemy tables and the real filesystem
capture — no mocked resolver surface. The ``db_env`` fixture mirrors
``test/clients/test_f631_terminal_identity.py``.
"""

import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    create_terminal,
    delete_terminal_and_warm_intent,
    get_frozen_pins,
    get_terminal_identity,
    get_terminal_identity_by_provider_session_id,
    register_provider_session,
)
from cli_agent_orchestrator.services import resume_service
from cli_agent_orchestrator.services.resume_service import (
    ResumeRefused,
    capture_kiro_session_id_from_store,
    provider_supports_resume,
    resolve_resume_target,
)

SESSION = "cao-resume"


@pytest.fixture
def db_env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.clear_terminal_metadata_cache()
    return sessions


def _make_lane(
    terminal_id="lane0001",
    *,
    provider="codex",
    uuid_value="uuid-lane0001",
    profile="codex_dev",
    cwd="/home/chao/repo",
):
    return create_terminal(
        terminal_id,
        SESSION,
        f"worker-{terminal_id}",
        provider,
        agent_profile=profile,
        working_directory=cwd,
        provider_session_id=uuid_value,
    )


# ── Deliverable 1: the resolver ─────────────────────────────────────────────


def test_resolve_live_terminal_id(db_env):
    _make_lane()
    row = resolve_resume_target("lane0001")
    assert row["session_uuid"] == "uuid-lane0001"
    assert row["provider"] == "codex"
    assert row["agent_profile"] == "codex_dev"
    assert row["cwd"] == "/home/chao/repo"
    assert row["source_terminal_id"] == "lane0001"


def test_resolve_reaped_terminal_id_survives_reap(db_env):
    """A reaped terminal id still resolves — the identity row outlives the reap."""
    _make_lane()
    delete_terminal_and_warm_intent("lane0001")
    assert get_terminal_identity("lane0001")["lifecycle"] == "reaped"
    row = resolve_resume_target("lane0001")
    assert row["session_uuid"] == "uuid-lane0001"
    assert row["provider"] == "codex"


def test_resolve_bare_uuid_via_identity_registry(db_env):
    _make_lane(uuid_value="uuid-bare-1")
    row = resolve_resume_target("uuid-bare-1")
    assert row["source_terminal_id"] == "lane0001"
    assert row["session_uuid"] == "uuid-bare-1"


def test_resolve_bare_uuid_via_provider_sessions_fallback(db_env):
    """A codex resume_key hand-registered as a fork base still resolves (§5)."""
    register_provider_session(
        name="base-x",
        provider="codex",
        session_uuid="11111111-1111-1111-1111-111111111111",
        cwd="/home/chao/repo",
        agent_profile="codex_dev",
        kind="base",
    )
    row = resolve_resume_target("11111111-1111-1111-1111-111111111111")
    assert row["session_uuid"] == "11111111-1111-1111-1111-111111111111"
    assert row["provider"] == "codex"
    assert row["cwd"] == "/home/chao/repo"


def test_resolve_unknown_refuses_with_identity(db_env):
    with pytest.raises(ResumeRefused) as exc:
        resolve_resume_target("nope-nothing")
    assert exc.value.missing == "identity"
    assert "nope-nothing" in exc.value.how


def test_resolve_identity_without_session_id_refuses_session_id(db_env):
    """A reaped kiro row whose id was never captured → missing=session_id."""
    create_terminal(
        "kiro0001",
        SESSION,
        "worker-kiro0001",
        "kiro_cli",
        agent_profile="kiro_dev",
        working_directory="/home/chao/repo",
        provider_session_id=None,
    )
    with pytest.raises(ResumeRefused) as exc:
        resolve_resume_target("kiro0001")
    assert exc.value.missing == "session_id"


def test_by_provider_session_id_accessor(db_env):
    _make_lane(uuid_value="uuid-accessor")
    row = get_terminal_identity_by_provider_session_id("uuid-accessor")
    assert row is not None and row["terminal_id"] == "lane0001"
    assert get_terminal_identity_by_provider_session_id("no-such-uuid") is None


# ── Deliverable 2: kiro resume capability + prefix rule ─────────────────────


def test_kiro_supports_resume_but_not_fork():
    from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

    assert KiroCliProvider.supports_resume is True
    assert KiroCliProvider.supports_fork_context is False
    assert provider_supports_resume("kiro_cli") is True


def test_codex_grok_resume_default_from_fork_axis():
    # supports_resume undeclared (None) → falls back to supports_fork_context.
    assert provider_supports_resume("codex") is True
    assert provider_supports_resume("grok_cli") is True


def test_unknown_provider_not_resumable():
    assert provider_supports_resume("no_such_provider") is False


def test_kiro_resume_id_is_verbatim_sess_prefix():
    """The kiro --resume-id value is the session id verbatim (sess_<uuid>).

    CAO adds no prefix: whatever the store recorded as ``id`` is passed
    through. build_kiro_command must append it unchanged.
    """
    from cli_agent_orchestrator.providers.kiro_capabilities import (
        KiroEngine,
        build_kiro_command,
    )

    sess = "sess_61e5f08f-04c9-4e35-97b8-ac27ae6a8ba4"
    argv = build_kiro_command(KiroEngine.KAS, "kiro_dev", yolo=True, resume_session_id=sess)
    assert "--resume-id" in argv
    assert argv[argv.index("--resume-id") + 1] == sess


# ── Deliverable 3: reap-time kiro capture from the on-disk store ────────────


def _write_kiro_session(root: Path, cwd: str, sess_id: str, created_at: str):
    hash_dir = root / resume_service._cwd_hash(cwd)
    sess_dir = hash_dir / sess_id
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "session.json").write_text(
        json.dumps(
            {
                "id": sess_id,
                "createdAt": created_at,
                "lastModifiedAt": created_at,
                "rootPaths": [cwd],
                "workspacePaths": [cwd],
            }
        ),
        encoding="utf-8",
    )
    return sess_dir


def test_capture_kiro_from_store_single_candidate(tmp_path):
    cwd = str(tmp_path / "wt")
    Path(cwd).mkdir()
    root = tmp_path / "sessions"
    _write_kiro_session(
        root, cwd, "sess_aaaaaaaa-0000-0000-0000-000000000001", "2026-09-08T04:00:00.000Z"
    )
    got = capture_kiro_session_id_from_store(cwd, launch_epoch=0.0, sessions_root=root)
    assert got == "sess_aaaaaaaa-0000-0000-0000-000000000001"


def test_capture_kiro_ignores_pre_launch_session(tmp_path):
    """A session created BEFORE the terminal launched must not be captured."""
    cwd = str(tmp_path / "wt")
    Path(cwd).mkdir()
    root = tmp_path / "sessions"
    _write_kiro_session(
        root, cwd, "sess_old-0000-0000-0000-000000000000", "2020-01-01T00:00:00.000Z"
    )
    import datetime as _dt

    launch = _dt.datetime(2026, 9, 8, 4, 0, tzinfo=_dt.timezone.utc).timestamp()
    got = capture_kiro_session_id_from_store(cwd, launch_epoch=launch, sessions_root=root)
    assert got is None


def test_capture_kiro_refuses_ambiguous(tmp_path):
    """Two post-launch sessions for the same cwd → refuse to guess (None)."""
    cwd = str(tmp_path / "wt")
    Path(cwd).mkdir()
    root = tmp_path / "sessions"
    _write_kiro_session(root, cwd, "sess_a-0000-0000-0000-000000000001", "2026-09-08T04:00:00.000Z")
    _write_kiro_session(root, cwd, "sess_b-0000-0000-0000-000000000002", "2026-09-08T04:05:00.000Z")
    got = capture_kiro_session_id_from_store(cwd, launch_epoch=0.0, sessions_root=root)
    assert got is None


def test_capture_kiro_no_store_returns_none(tmp_path):
    got = capture_kiro_session_id_from_store(
        str(tmp_path / "missing"), launch_epoch=0.0, sessions_root=tmp_path / "sessions"
    )
    assert got is None


def test_reap_fills_kiro_session_id_and_returns_resume_key(db_env):
    """A kiro reap with a captured id writes it into the identity row and
    returns it as resume_key."""
    create_terminal(
        "kiro0002",
        SESSION,
        "worker-kiro0002",
        "kiro_cli",
        agent_profile="kiro_dev",
        working_directory="/home/chao/repo",
        provider_session_id=None,
    )
    result = delete_terminal_and_warm_intent(
        "kiro0002",
        captured_provider_session_id="sess_captured-0000-0000-0000-000000000009",
    )
    assert result["resume_key"] == "sess_captured-0000-0000-0000-000000000009"
    assert (
        get_terminal_identity("kiro0002")["provider_session_id"]
        == "sess_captured-0000-0000-0000-000000000009"
    )


def test_reap_resume_hint_when_no_key(db_env):
    create_terminal(
        "kiro0003",
        SESSION,
        "worker-kiro0003",
        "kiro_cli",
        agent_profile="kiro_dev",
        working_directory="/home/chao/repo",
        provider_session_id=None,
    )
    result = delete_terminal_and_warm_intent("kiro0003", resume_hint="no session persisted")
    assert result["resume_key"] is None
    assert result["resume_hint"] == "no session persisted"


def test_reap_does_not_overwrite_existing_session_id(db_env):
    _make_lane("codex0004", provider="codex", uuid_value="uuid-existing")
    result = delete_terminal_and_warm_intent(
        "codex0004", captured_provider_session_id="should-not-apply"
    )
    assert result["resume_key"] == "uuid-existing"


def test_worktree_path_recorded_at_create(db_env):
    create_terminal(
        "wtree0005",
        SESSION,
        "worker-wtree0005",
        "kiro_cli",
        agent_profile="kiro_dev",
        working_directory="/data/x/.cao/worktrees/wtree0005",
        worktree_info={
            "repo_root": "/data/x",
            "worktree_path": "/data/x/.cao/worktrees/wtree0005",
            "expected_branch": "cao/wtree0005",
            "terminal_id": "wtree0005",
        },
    )
    assert get_terminal_identity("wtree0005")["worktree_path"] == "/data/x/.cao/worktrees/wtree0005"


# ── Deliverable 3c: worktree kept when branch has unmerged commits ──────────


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)
    (path / "f.txt").write_text("base\n")
    _git(["add", "."], path)
    _git(["commit", "-qm", "base"], path)


def test_worktree_kept_when_branch_has_unmerged_commits(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import worktree_service

    monkeypatch.setenv("CAO_WORKTREE_ROOT", str(tmp_path / "wts"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = worktree_service.create_worktree(str(repo), "term9001")
    # Commit real work on the worktree branch (unmerged w.r.t. repo HEAD).
    (Path(wt) / "work.txt").write_text("progress\n")
    _git(["add", "."], wt)
    _git(["commit", "-qm", "worker progress"], wt)

    worktree_service.remove_worktree(str(repo), "term9001", worktree_path=wt)
    # Directory kept because the branch carries unmerged commits.
    assert Path(wt).is_dir()
    assert worktree_service._branch_has_unmerged_commits(str(repo), "cao/term9001")


def test_worktree_removed_when_branch_clean(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import worktree_service

    monkeypatch.setenv("CAO_WORKTREE_ROOT", str(tmp_path / "wts"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = worktree_service.create_worktree(str(repo), "term9002")
    # No commits on the branch → safe to remove (today's behaviour).
    worktree_service.remove_worktree(str(repo), "term9002", worktree_path=wt)
    assert not Path(wt).is_dir()


# ── Deliverable 4: the ONE typed refusal shape ──────────────────────────────


def test_resume_refused_shape():
    r = ResumeRefused(missing="cwd", how="do X")
    assert r.as_dict() == {"error": "resume_refused", "missing": "cwd", "how": "do X"}


def test_resume_refused_rejects_unknown_token():
    with pytest.raises(ValueError):
        ResumeRefused(missing="not-a-token", how="x")


# ── inherit_pins ────────────────────────────────────────────────────────────


def _seed_frozen_pin(task_key: str, file_path: str, sha: str, version: int = 1):
    """Insert a frozen authority pin row directly (bypass the file-hash check —
    ``get_frozen_pins`` reads the table, and inherit_pins re-declares by sha)."""
    from cli_agent_orchestrator.clients.database import AuthorityPinModel, SessionLocal

    with SessionLocal() as db:
        db.add(
            AuthorityPinModel(
                task_key=task_key,
                file_path=file_path,
                sha256=sha,
                version=version,
                registered_by="test",
                frozen=True,
            )
        )
        db.commit()


def test_get_frozen_pins_returns_latest_shas(db_env):
    _make_lane("pinlane01", provider="codex", uuid_value="uuid-pin")
    _seed_frozen_pin("pinlane01", "/a/b.md", "a" * 64, version=1)
    _seed_frozen_pin("pinlane01", "/a/b.md", "c" * 64, version=2)  # latest wins
    pins = get_frozen_pins("pinlane01")
    assert pins == [{"file_path": "/a/b.md", "sha256": "c" * 64}]


def test_prepare_resume_inherit_pins(db_env, tmp_path):
    """prepare_resume(inherit_pins=True) copies the reaped pin set forward."""
    cwd = str(tmp_path)
    _make_lane("pinlane02", provider="codex", uuid_value="uuid-pin2", cwd=cwd)
    _seed_frozen_pin("pinlane02", "/a/b.md", "b" * 64)
    out = resume_service.prepare_resume(
        resume_from="pinlane02",
        requested_agent_profile=None,
        requested_working_directory=cwd,
        inherit_pins=True,
    )
    assert out["authority_files"] == [{"file_path": "/a/b.md", "sha256": "b" * 64}]
    assert out["fork_context"].mode == "resume"
    assert out["provider"] == "codex"
