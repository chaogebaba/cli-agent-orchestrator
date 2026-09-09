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


def test_resolve_bare_uuid_provider_sessions_NOT_consulted(db_env):
    """r2 #1: resume never reads the fork-base catalog. A uuid that exists ONLY
    as a provider_sessions row (no identity) → resume_refused.identity."""
    register_provider_session(
        name="base-x",
        provider="codex",
        session_uuid="11111111-1111-1111-1111-111111111111",
        cwd="/home/chao/repo",
        agent_profile="codex_dev",
        kind="base",
    )
    with pytest.raises(ResumeRefused) as exc:
        resolve_resume_target("11111111-1111-1111-1111-111111111111")
    assert exc.value.missing == "identity"


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


def test_capability_only_codex_and_kiro(db_env):
    # r1 #6 / r2 #2: supports_resume True ONLY for codex + kiro this slice;
    # grok/claude/pi False (grok has supports_fork_context=True but resume=False).
    assert provider_supports_resume("codex") is True
    assert provider_supports_resume("kiro_cli") is True
    assert provider_supports_resume("grok_cli") is False
    from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider

    assert GrokCliProvider.supports_fork_context is True  # fork axis unchanged
    assert GrokCliProvider.supports_resume is False


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


def _write_kiro_session(
    root: Path, cwd: str, sess_id: str, *, marker: str = "", created_at="2026-09-08T04:00:00.000Z"
):
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
    # messages.jsonl carries the per-terminal assign-trailer marker used for
    # positive attribution (r1 #2).
    (sess_dir / "messages.jsonl").write_text(
        json.dumps({"role": "user", "content": f"task…\n{marker}"}) + "\n" if marker else "{}\n",
        encoding="utf-8",
    )
    return sess_dir


def test_capture_kiro_positive_attribution_single(tmp_path):
    """r1 #2: binds on the terminal's own assign-trailer marker, one match."""
    cwd = str(tmp_path / "wt")
    Path(cwd).mkdir()
    root = tmp_path / "sessions"
    _write_kiro_session(
        root,
        cwd,
        "sess_aaaaaaaa-0000-0000-0000-000000000001",
        marker="[Assigned by terminal abcd1234. When done…]",
    )
    got, reason, count = capture_kiro_session_id_from_store(cwd, "abcd1234", sessions_root=root)
    assert got == "sess_aaaaaaaa-0000-0000-0000-000000000001"
    assert reason is None and count == 1


def test_capture_kiro_recorded_locator_wins(tmp_path):
    got, reason, count = capture_kiro_session_id_from_store(
        str(tmp_path), "abcd1234", recorded_locator="sess_recorded-1", sessions_root=tmp_path / "s"
    )
    assert got == "sess_recorded-1" and reason is None


def test_capture_kiro_no_marker_match_refuses(tmp_path):
    """A cwd-matching session WITHOUT this terminal's marker is NOT captured
    (never cwd-alone)."""
    cwd = str(tmp_path / "wt")
    Path(cwd).mkdir()
    root = tmp_path / "sessions"
    _write_kiro_session(
        root,
        cwd,
        "sess_other-0000-0000-0000-000000000001",
        marker="[Assigned by terminal SOMEONELSE. …]",
    )
    got, reason, count = capture_kiro_session_id_from_store(cwd, "abcd1234", sessions_root=root)
    assert got is None and reason == "capture_unknown" and count == 1


def test_capture_kiro_ambiguous_two_markers_refuses(tmp_path):
    """Two sessions both carrying this terminal's marker → refuse to guess."""
    cwd = str(tmp_path / "wt")
    Path(cwd).mkdir()
    root = tmp_path / "sessions"
    m = "[Assigned by terminal abcd1234. …]"
    _write_kiro_session(root, cwd, "sess_a-0000-0000-0000-000000000001", marker=m)
    _write_kiro_session(root, cwd, "sess_b-0000-0000-0000-000000000002", marker=m)
    got, reason, count = capture_kiro_session_id_from_store(cwd, "abcd1234", sessions_root=root)
    assert got is None and reason == "capture_unknown" and count == 2


def test_capture_kiro_no_store_returns_none(tmp_path):
    got, reason, count = capture_kiro_session_id_from_store(
        str(tmp_path / "missing"), "abcd1234", sessions_root=tmp_path / "sessions"
    )
    assert got is None and reason == "capture_unknown" and count == 0


# ── verdict B4: per-attempt capture_nonce positive attribution ──────────────


def test_capture_kiro_nonce_two_same_cwd_candidates_returns_own(tmp_path):
    """B4: two sessions under the SAME cwd — one carries THIS attempt's
    capture_nonce, the other is a foreign/copied session (newer mtime, and even
    carrying the copyable assign-trailer). The nonce selector returns the OWN
    session, never the newest."""
    cwd = str(tmp_path / "wt")
    Path(cwd).mkdir()
    root = tmp_path / "sessions"
    nonce = "cao-nonce-deadbeefdeadbeefdeadbeefdeadbeef"
    # OWN session: carries the per-attempt nonce (older mtime).
    _write_kiro_session(
        root,
        cwd,
        "sess_own-0000-0000-0000-000000000001",
        marker=f"task…\n<!-- cao-capture-nonce: {nonce} -->",
        created_at="2026-09-08T04:00:00.000Z",
    )
    # FOREIGN session: newer, and even carries the copyable per-terminal
    # assign-trailer — the OLD newest/trailer heuristics would wrongly pick it.
    _write_kiro_session(
        root,
        cwd,
        "sess_foreign-0000-0000-0000-000000000002",
        marker="[Assigned by terminal abcd1234. …]",
        created_at="2026-09-09T09:00:00.000Z",
    )
    got, reason, count = capture_kiro_session_id_from_store(
        cwd, "abcd1234", capture_nonce=nonce, sessions_root=root
    )
    assert got == "sess_own-0000-0000-0000-000000000001"
    assert reason is None and count == 2


def test_capture_kiro_nonce_no_carrier_refuses(tmp_path):
    """When the nonce is set but NO candidate carries it, refuse (never fall back
    to newest/mtime)."""
    cwd = str(tmp_path / "wt")
    Path(cwd).mkdir()
    root = tmp_path / "sessions"
    _write_kiro_session(
        root,
        cwd,
        "sess_x-0000-0000-0000-000000000001",
        marker="[Assigned by terminal abcd1234. …]",  # trailer present, nonce absent
    )
    got, reason, count = capture_kiro_session_id_from_store(
        cwd, "abcd1234", capture_nonce="cao-nonce-notpresent", sessions_root=root
    )
    assert got is None and reason == "capture_unknown"


def test_capture_kiro_nonce_falls_back_to_trailer_when_no_nonce(tmp_path):
    """Backward compat: with NO nonce recorded (legacy row), the per-terminal
    assign-trailer marker still positively attributes — still never mtime."""
    cwd = str(tmp_path / "wt")
    Path(cwd).mkdir()
    root = tmp_path / "sessions"
    _write_kiro_session(
        root,
        cwd,
        "sess_legacy-0000-0000-0000-000000000001",
        marker="[Assigned by terminal abcd1234. …]",
    )
    got, reason, count = capture_kiro_session_id_from_store(
        cwd, "abcd1234", capture_nonce=None, sessions_root=root
    )
    assert got == "sess_legacy-0000-0000-0000-000000000001" and reason is None


def test_reap_fills_kiro_session_id_and_returns_full_block(db_env):
    """A kiro reap with a captured id writes it into the identity row; the reap
    block reports resume_key=<historical terminal id> + provider_session_id."""
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
        resumable=True,
        resume_reason="resumable",
    )
    assert result["resume_key"] == "kiro0002"  # historical terminal id
    assert result["provider_session_id"] == "sess_captured-0000-0000-0000-000000000009"
    assert result["resumable"] is True
    assert result["reason"] == "resumable"
    assert (
        get_terminal_identity("kiro0002")["provider_session_id"]
        == "sess_captured-0000-0000-0000-000000000009"
    )


def test_reap_block_when_no_capture(db_env):
    create_terminal(
        "kiro0003",
        SESSION,
        "worker-kiro0003",
        "kiro_cli",
        agent_profile="kiro_dev",
        working_directory="/home/chao/repo",
        provider_session_id=None,
    )
    result = delete_terminal_and_warm_intent(
        "kiro0003", resumable=False, resume_reason="capture_unknown_candidates_0"
    )
    assert result["resume_key"] == "kiro0003"
    assert result["provider_session_id"] is None
    assert result["resumable"] is False
    assert result["reason"] == "capture_unknown_candidates_0"


def test_reap_returns_resume_key_as_terminal_id_for_codex(db_env):
    _make_lane("codex0004", provider="codex", uuid_value="uuid-existing")
    result = delete_terminal_and_warm_intent("codex0004", resumable=True, resume_reason="resumable")
    # r1 #4: resume_key is the historical terminal id; the uuid is provider_session_id.
    assert result["resume_key"] == "codex0004"
    assert result["provider_session_id"] == "uuid-existing"
    assert result["resumable"] is True


def test_reap_does_not_overwrite_existing_session_id(db_env):
    _make_lane("codex0006", provider="codex", uuid_value="uuid-keep")
    result = delete_terminal_and_warm_intent(
        "codex0006", captured_provider_session_id="should-not-apply"
    )
    assert result["provider_session_id"] == "uuid-keep"


def test_worktree_path_and_branch_recorded_at_create(db_env):
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
    idn = get_terminal_identity("wtree0005")
    assert idn["worktree_path"] == "/data/x/.cao/worktrees/wtree0005"
    assert idn["worktree_branch"] == "cao/wtree0005"


# ── Addendum r1 #3: worktree retained on normal reap; removed only on abandon ─


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


def test_remove_worktree_on_abandon_discards_checkout(tmp_path, monkeypatch):
    """remove_worktree is the ABANDON path (force delete): it discards the
    checkout; the branch is safe-deleted so committed work survives as a branch."""
    from cli_agent_orchestrator.services import worktree_service

    monkeypatch.setenv("CAO_WORKTREE_ROOT", str(tmp_path / "wts"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = worktree_service.create_worktree(str(repo), "term9002")
    worktree_service.remove_worktree(str(repo), "term9002", worktree_path=wt)
    assert not Path(wt).is_dir()


def test_branch_has_unmerged_commits_helper(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import worktree_service

    monkeypatch.setenv("CAO_WORKTREE_ROOT", str(tmp_path / "wts"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = worktree_service.create_worktree(str(repo), "term9001")
    (Path(wt) / "work.txt").write_text("progress\n")
    _git(["add", "."], wt)
    _git(["commit", "-qm", "worker progress"], wt)
    assert worktree_service._branch_has_unmerged_commits(str(repo), "cao/term9001")


# ── Deliverable 4: the ONE typed refusal shape (r1 #7) ──────────────────────


def test_resume_refused_shape():
    r = ResumeRefused(missing="cwd", how="do X", reason="cwd_missing_no_provenance")
    assert r.as_dict() == {
        "error": "resume_refused",
        "missing": "cwd",
        "how": "do X",
        "reason": "cwd_missing_no_provenance",
        "retryable": False,
    }


def test_resume_refused_rejects_unknown_token():
    with pytest.raises(ValueError):
        ResumeRefused(missing="not-a-token", how="x", reason="r")


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


def test_prepare_resume_inherit_pins_defaults_true(db_env, tmp_path):
    """r1 #5: inherit_pins defaults True — the reaped pin set is carried."""
    cwd = str(tmp_path)
    _make_lane("pinlane03", provider="codex", uuid_value="uuid-pin3", cwd=cwd)
    _seed_frozen_pin("pinlane03", "/a/b.md", "d" * 64)
    out = resume_service.prepare_resume(
        resume_from="pinlane03",
        requested_agent_profile=None,
        requested_working_directory=cwd,
        # inherit_pins omitted → default True
    )
    assert out["pins_inherited"] == 1
    assert out["authority_files"] == [{"file_path": "/a/b.md", "sha256": "d" * 64}]


def test_prepare_resume_inherit_pins_false_reports_known_pins(db_env, tmp_path):
    """r1 #5: inherit_pins=False surfaces known_pins so the handler can refuse
    when no replacement authority_files are supplied."""
    cwd = str(tmp_path)
    _make_lane("pinlane04", provider="codex", uuid_value="uuid-pin4", cwd=cwd)
    _seed_frozen_pin("pinlane04", "/a/b.md", "e" * 64)
    out = resume_service.prepare_resume(
        resume_from="pinlane04",
        requested_agent_profile=None,
        requested_working_directory=cwd,
        inherit_pins=False,
    )
    assert out["authority_files"] is None
    assert out["known_pins"] == [{"file_path": "/a/b.md", "sha256": "e" * 64}]


def test_prepare_resume_grok_refuses_provider_capability(db_env, tmp_path):
    """r2 #2: grok is not resumable this slice → provider_capability refusal."""
    cwd = str(tmp_path)
    _make_lane("grok0007", provider="grok_cli", uuid_value="uuid-grok", cwd=cwd)
    with pytest.raises(ResumeRefused) as exc:
        resume_service.prepare_resume(
            resume_from="grok0007",
            requested_agent_profile=None,
            requested_working_directory=cwd,
            inherit_pins=True,
        )
    assert exc.value.missing == "provider_capability"
    assert "F829 build 2" in exc.value.how


def test_prepare_resume_cwd_missing_no_provenance_refuses(db_env):
    """r1 #7: a gone cwd with no recorded worktree provenance → missing=cwd."""
    _make_lane("codex0008", provider="codex", uuid_value="uuid-8", cwd="/gone/nowhere-xyz")
    with pytest.raises(ResumeRefused) as exc:
        resume_service.prepare_resume(
            resume_from="codex0008",
            requested_agent_profile=None,
            requested_working_directory=None,
            inherit_pins=True,
        )
    assert exc.value.missing == "cwd"
    assert exc.value.reason == "cwd_missing_no_provenance"


def test_prepare_resume_cwd_reconstructs_from_provenance(db_env, tmp_path, monkeypatch):
    """r1 #7: a gone cwd IS reconstructed from recorded path+branch+commit."""
    from cli_agent_orchestrator.services import worktree_service

    monkeypatch.setenv("CAO_WORKTREE_ROOT", str(tmp_path / "wts"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = worktree_service.create_worktree(str(repo), "recon01")
    # Commit work so the branch survives the abandon (git branch -d refuses).
    (Path(wt) / "progress.txt").write_text("work\n")
    _git(["add", "."], wt)
    _git(["commit", "-qm", "progress"], wt)
    commit = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    # Register an identity with the worktree provenance, then remove the checkout.
    create_terminal(
        "recon01",
        SESSION,
        "worker-recon01",
        "codex",
        agent_profile="codex_dev",
        working_directory=wt,
        provider_session_id="uuid-recon",
        worktree_info={
            "repo_root": str(repo),
            "worktree_path": wt,
            "expected_branch": "cao/recon01",
            "terminal_id": "recon01",
        },
    )
    # Record the commit on the identity row (git_sha).
    from cli_agent_orchestrator.clients.database import SessionLocal, TerminalIdentityModel

    with SessionLocal() as db:
        row = db.query(TerminalIdentityModel).filter_by(terminal_id="recon01").one()
        row.git_sha = commit
        db.commit()
    # Remove the checkout (simulate abandon/GC) but keep the branch.
    worktree_service.remove_worktree(str(repo), "recon01", worktree_path=wt)
    assert not Path(wt).is_dir()

    out = resume_service.prepare_resume(
        resume_from="recon01",
        requested_agent_profile=None,
        requested_working_directory=None,
        inherit_pins=True,
    )
    assert out["working_directory"] == wt
    assert Path(wt).is_dir()  # reconstructed at the exact path
