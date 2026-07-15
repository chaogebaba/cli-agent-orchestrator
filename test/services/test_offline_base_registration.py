"""Wave 2 strict offline-base registration acceptance tests."""

import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.mcp_server.server import _assign_impl
from cli_agent_orchestrator.services import fork_context_service as svc


REJECT_CODES = (
    "name_reserved",
    "provider_unknown",
    "fork_unsupported",
    "cwd_not_absolute",
    "uuid_malformed",
    "profile_unknown",
    "profile_provider_mismatch",
    "artifact_not_found",
    "artifact_ambiguous",
    "artifact_identity_mismatch",
    "artifact_cwd_mismatch",
    "cwd_not_git_worktree",
)


@pytest.fixture
def registry(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'registry.db'}")
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    return engine


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def profiles(monkeypatch):
    rows = {
        "codex_profile": SimpleNamespace(provider="codex"),
        "grok_profile": SimpleNamespace(provider="grok_cli"),
        "wrong_profile": SimpleNamespace(provider="grok_cli"),
    }

    def load(name):
        if name not in rows:
            raise FileNotFoundError(name)
        return rows[name]

    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile", load
    )
    return rows


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / f"repo-{uuid.uuid4()}"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=Test", "-c",
            "user.email=test@example.invalid", "commit", "-qm", "base",
        ],
        cwd=repo,
        check=True,
    )
    return repo


def _write_codex(
    home: Path, session_uuid: str, cwd: Path, *, payload_id: str | None = None,
    payload_cwd: Path | None = None, bucket: str = "one",
) -> Path:
    path = home / ".codex" / "sessions" / bucket / f"rollout-{session_uuid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {
                "id": payload_id or session_uuid,
                "cwd": str(payload_cwd or cwd),
            },
        }) + "\n",
        encoding="utf-8",
    )
    return path


def _write_grok(home: Path, session_uuid: str, cwd: Path) -> Path:
    path = (
        home / ".grok" / "sessions" / quote(str(cwd.resolve()), safe="")
        / session_uuid / "chat_history.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"role":"user"}\n', encoding="utf-8")
    return path


@pytest.mark.parametrize("expected_code", REJECT_CODES)
def test_exhaustive_registration_reject_table(
    expected_code, fake_home, profiles, tmp_path
):
    repo = _git_repo(tmp_path)
    session_uuid = str(uuid.uuid4())
    name = "base"
    provider = "codex"
    cwd = str(repo)
    profile = "codex_profile"

    if expected_code == "name_reserved":
        name = "cold"
    elif expected_code == "provider_unknown":
        provider = "missing_provider"
    elif expected_code == "fork_unsupported":
        provider = "kiro_cli"
    elif expected_code == "cwd_not_absolute":
        cwd = "relative/repo"
    elif expected_code == "uuid_malformed":
        session_uuid = "not-a-uuid"
    elif expected_code == "profile_unknown":
        profile = "missing_profile"
    elif expected_code == "profile_provider_mismatch":
        profile = "wrong_profile"
    elif expected_code == "artifact_ambiguous":
        _write_codex(fake_home, session_uuid, repo, bucket="one")
        _write_codex(fake_home, session_uuid, repo, bucket="two")
    elif expected_code == "artifact_identity_mismatch":
        _write_codex(fake_home, session_uuid, repo, payload_id=str(uuid.uuid4()))
    elif expected_code == "artifact_cwd_mismatch":
        _write_codex(fake_home, session_uuid, repo, payload_cwd=tmp_path / "other")
    elif expected_code == "cwd_not_git_worktree":
        nongit = tmp_path / "nongit"
        nongit.mkdir()
        cwd = str(nongit)
        _write_codex(fake_home, session_uuid, nongit)
    elif expected_code != "artifact_not_found":
        raise AssertionError(f"unhandled code: {expected_code}")

    with pytest.raises(svc.OfflineBaseRegistrationError) as caught:
        svc.validate_base_source(
            mode="registration", name=name, provider=provider,
            session_uuid=session_uuid, cwd=cwd, agent_profile=profile,
        )
    assert caught.value.code == expected_code


@pytest.mark.parametrize(
    ("provider", "profile", "write_artifact"),
    [
        ("codex", "codex_profile", _write_codex),
        ("grok_cli", "grok_profile", _write_grok),
    ],
)
def test_valid_registration_projects_global_row(
    provider, profile, write_artifact, registry, fake_home, profiles, tmp_path
):
    repo = _git_repo(tmp_path)
    session_uuid = str(uuid.uuid4())
    write_artifact(fake_home, session_uuid, repo)

    result = svc.register_offline_base(
        name=f"{provider}-base", provider=provider, session_uuid=session_uuid,
        cwd=str(repo), agent_profile=profile, summary="offline",
    )

    assert result == {
        "name": f"{provider}-base",
        "provider": provider,
        "profile": profile,
        "cwd": str(repo.resolve()),
        "session_uuid": session_uuid,
        "kind": "base",
        "session_name": None,
        "source_terminal_id": None,
        "git_sha": result["git_sha"],
        "dirty_hashes": "{}",
        "superseded": False,
    }
    assert isinstance(result["git_sha"], str) and result["git_sha"]


def test_validation_precedes_supersession(
    registry, fake_home, profiles, tmp_path
):
    repo = _git_repo(tmp_path)
    old_uuid = str(uuid.uuid4())
    old = database.register_provider_session(
        name="stable", provider="codex", session_uuid=old_uuid, cwd=str(repo),
        agent_profile="codex_profile", git_sha="a" * 40, dirty_hashes="{}",
        kind="base", source_terminal_id=None, session_name=None,
    )
    missing_uuid = str(uuid.uuid4())

    with pytest.raises(svc.OfflineBaseRegistrationError) as caught:
        svc.register_offline_base(
            name="stable", provider="codex", session_uuid=missing_uuid,
            cwd=str(repo), agent_profile="codex_profile",
        )
    assert caught.value.code == "artifact_not_found"
    assert database.get_ready_provider_session("stable")["id"] == old["id"]

    _write_codex(fake_home, missing_uuid, repo)
    replacement = svc.register_offline_base(
        name="stable", provider="codex", session_uuid=missing_uuid,
        cwd=str(repo), agent_profile="codex_profile",
    )
    assert replacement["superseded"] is True
    assert database.get_ready_provider_session("stable")["session_uuid"] == missing_uuid


def test_non_utf8_codex_rollout_is_stable_service_reject(
    fake_home, profiles, tmp_path
):
    repo = _git_repo(tmp_path)
    session_uuid = str(uuid.uuid4())
    rollout = _write_codex(fake_home, session_uuid, repo)
    rollout.write_bytes(b"\xff\xfe\x80")

    with pytest.raises(svc.OfflineBaseRegistrationError) as caught:
        svc.validate_base_source(
            mode="registration", name="invalid-utf8", provider="codex",
            session_uuid=session_uuid, cwd=str(repo),
            agent_profile="codex_profile",
        )
    assert caught.value.code == "artifact_identity_mismatch"


def test_non_utf8_codex_rollout_is_stable_api_400(
    registry, fake_home, profiles, tmp_path
):
    from cli_agent_orchestrator.api.main import app

    repo = _git_repo(tmp_path)
    session_uuid = str(uuid.uuid4())
    rollout = _write_codex(fake_home, session_uuid, repo)
    rollout.write_bytes(b"\xff\xfe\x80")

    response = TestClient(app).post(
        "/bases/register",
        headers={"Host": "localhost"},
        json={
            "name": "invalid-utf8",
            "provider": "codex",
            "session_uuid": session_uuid,
            "cwd": str(repo),
            "profile": "codex_profile",
        },
    )
    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "artifact_identity_mismatch",
            "message": "provider artifact identity could not be validated",
        }
    }


def test_fresh_then_mutate_then_stale_assign_succeeds_quietly(
    registry, fake_home, profiles, tmp_path, monkeypatch
):
    repo = _git_repo(tmp_path)
    session_uuid = str(uuid.uuid4())
    _write_codex(fake_home, session_uuid, repo)
    svc.register_offline_base(
        name="offline", provider="codex", session_uuid=session_uuid,
        cwd=str(repo), agent_profile="codex_profile",
    )
    row = database.get_ready_provider_session("offline")
    assert row["source_terminal_id"] is None and row["session_name"] is None
    assert svc.staleness(row)[0] == []

    (repo / "tracked.txt").write_text("mutated\n", encoding="utf-8")
    changed, preamble = svc.staleness(row)
    assert changed == ["tracked.txt"]
    assert preamble.startswith("[STALE]")

    monkeypatch.setenv("CAO_TERMINAL_ID", "super001")
    with (
        patch(
            "cli_agent_orchestrator.mcp_server.server.resolve_provider",
            return_value="codex",
        ),
        patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal",
            return_value=("worker01", "codex"),
        ) as create,
    ):
        result = _assign_impl("codex_profile", "task", fork_from="offline")

    assert result["success"] is True
    kwargs = create.call_args.kwargs
    assert kwargs["fork_context"].session_uuid == session_uuid
    assert kwargs["fork_context"].initial_preamble.startswith("[STALE]")
    assert "dead-source" not in kwargs["fork_context"].initial_preamble
    assert kwargs["refresh_base_name"] == "offline"
