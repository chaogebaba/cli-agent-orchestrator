from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.utils import persona_context
from cli_agent_orchestrator.utils.persona_context import (
    PersonaRetentionIntent,
    reconcile_retained_persona_homes,
    resolve_codex_home,
    retain_codex_persona_home,
)
from cli_agent_orchestrator.utils.provider_plane import ProviderHome

UUID_ONE = "11111111-1111-4111-8111-111111111111"
UUID_TWO = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def retention_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'provider.db'}", connect_args={"check_same_thread": False}
    )
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    (runtime / "cao-personas").mkdir(mode=0o700)
    (runtime / "cao-personas").chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("CAO_INSTANCE_ID", raising=False)
    return runtime


def _values(
    name: str,
    session_uuid: str = UUID_ONE,
    terminal_id: str | None = "terminal-one",
) -> dict[str, object]:
    return {
        "name": name,
        "provider": "codex",
        "session_uuid": session_uuid,
        "cwd": "/repo",
        "agent_profile": "developer",
        "git_sha": "a" * 40,
        "dirty_hashes": "{}",
        "summary": name,
        "source_terminal_id": terminal_id,
        "session_name": "cao-test",
    }


def _rows(session_uuid: str) -> list[database.ProviderSessionModel]:
    with database.SessionLocal() as db:
        return (
            db.query(database.ProviderSessionModel)
            .filter_by(session_uuid=session_uuid)
            .order_by(database.ProviderSessionModel.id)
            .all()
        )


def test_alias_claim_inheritance_and_last_owner_cleanup(retention_env: Path) -> None:
    database.register_provider_session(**_values("first"))
    database.register_provider_session(**_values("alias", terminal_id="terminal-alias"))
    destination = retention_env / "cao-personas" / "retained" / UUID_ONE
    destination.mkdir(parents=True)
    assert database.claim_retained_persona_home(UUID_ONE, str(destination)) == 2

    inherited = database.register_provider_session(
        **_values("late-alias", terminal_id="terminal-late")
    )
    assert inherited["retained_persona_home"] == str(destination)
    database.retire_provider_session("first")
    assert destination.is_dir()
    database.retire_provider_session("alias")
    assert destination.is_dir()
    database.retire_provider_session("late-alias")
    assert not destination.exists()
    assert all(row.retained_persona_home is None for row in _rows(UUID_ONE))


def test_resolver_finds_retained_home_after_live_manifest_is_gone(
    retention_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.register_provider_session(**_values("base"))
    destination = retention_env / "cao-personas" / "retained" / UUID_ONE
    destination.mkdir(parents=True)
    database.claim_retained_persona_home(UUID_ONE, str(destination))
    production = retention_env / "production-codex"
    production.mkdir()
    monkeypatch.setattr(persona_context, "load_persona_plan", lambda terminal_id: None)
    monkeypatch.setattr(
        persona_context,
        "provider_home",
        lambda provider: ProviderHome(provider, "production", production),
    )
    # A RETAINED home outranks the ambient environment — it is the home this
    # terminal's own codex was launched with. Asserted with the suite's F703
    # (#558) CODEX_HOME pin still in force, which is the point.
    assert resolve_codex_home("terminal-one") == destination
    # The two fallback legs are what CODEX_HOME redirects, so drop the pin to
    # reach production underneath it.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert resolve_codex_home(None) == production
    assert resolve_codex_home("unknown-terminal") == production


def test_same_uuid_refresh_transfers_claim_then_retire_cleans(retention_env: Path) -> None:
    database.register_provider_session(**_values("base"))
    destination = retention_env / "cao-personas" / "retained" / UUID_ONE
    destination.mkdir(parents=True)
    database.claim_retained_persona_home(UUID_ONE, str(destination))
    replacement = database.register_provider_session(
        **_values("base", terminal_id="terminal-refresh")
    )
    assert replacement["retained_persona_home"] == str(destination)
    history = _rows(UUID_ONE)
    assert [row.status for row in history] == ["superseded", "ready"]
    assert history[0].retained_persona_home is None
    database.retire_provider_session("base")
    assert not destination.exists()


def test_different_uuid_supersession_removes_old_home(retention_env: Path) -> None:
    database.register_provider_session(**_values("base"))
    destination = retention_env / "cao-personas" / "retained" / UUID_ONE
    destination.mkdir(parents=True)
    database.claim_retained_persona_home(UUID_ONE, str(destination))
    replacement = database.register_provider_session(
        **_values("base", session_uuid=UUID_TWO, terminal_id="terminal-two")
    )
    assert replacement["session_uuid"] == UUID_TWO
    assert replacement["retained_persona_home"] is None
    assert not destination.exists()
    assert all(row.retained_persona_home is None for row in _rows(UUID_ONE))


def test_claim_then_move_and_post_rename_verification(
    retention_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.register_provider_session(**_values("base"))
    source = retention_env / "source-codex-home"
    source.mkdir()
    (source / "auth.json").write_text("auth", encoding="utf-8")
    destination = retention_env / "cao-personas" / "retained" / UUID_ONE
    monkeypatch.setattr(
        persona_context,
        "load_persona_plan",
        lambda terminal_id: SimpleNamespace(provider="codex", codex_home=source),
    )
    error = retain_codex_persona_home(
        "terminal-one", PersonaRetentionIntent(UUID_ONE, destination, (1,))
    )
    assert error is None
    assert destination.joinpath("auth.json").is_file()
    assert database.verify_retained_persona_claim(UUID_ONE, str(destination)) == 1


def test_move_failure_unclaims_and_degrades_to_production(
    retention_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.register_provider_session(**_values("base"))
    source = retention_env / "source-codex-home"
    source.mkdir()
    destination = retention_env / "cao-personas" / "retained" / UUID_ONE
    monkeypatch.setattr(
        persona_context,
        "load_persona_plan",
        lambda terminal_id: SimpleNamespace(provider="codex", codex_home=source),
    )
    monkeypatch.setattr(os, "rename", lambda source, destination: (_ for _ in ()).throw(OSError()))
    error = retain_codex_persona_home(
        "terminal-one", PersonaRetentionIntent(UUID_ONE, destination, (1,))
    )
    assert error == "retained_persona_move_failed"
    assert source.is_dir()
    assert not destination.exists()
    assert database.get_ready_provider_session("base")["retained_persona_home"] is None


def test_retire_inside_claim_window_forces_post_rename_compensation(
    retention_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.register_provider_session(**_values("base"))
    source = retention_env / "source-codex-home"
    source.mkdir()
    destination = retention_env / "cao-personas" / "retained" / UUID_ONE
    monkeypatch.setattr(
        persona_context,
        "load_persona_plan",
        lambda terminal_id: SimpleNamespace(provider="codex", codex_home=source),
    )
    real_rename = os.rename

    def retire_during_rename(source_path: Path, destination_path: Path) -> None:
        database.retire_provider_session("base")
        real_rename(source_path, destination_path)

    monkeypatch.setattr(os, "rename", retire_during_rename)
    assert (
        retain_codex_persona_home(
            "terminal-one", PersonaRetentionIntent(UUID_ONE, destination, (1,))
        )
        is None
    )
    assert not destination.exists()
    assert database.verify_retained_persona_claim(UUID_ONE, str(destination)) == 0


def test_zero_owner_fence_prevents_alias_reclaim(retention_env: Path) -> None:
    database.register_provider_session(**_values("base"))
    destination = retention_env / "cao-personas" / "retained" / UUID_ONE
    destination.mkdir(parents=True)
    database.claim_retained_persona_home(UUID_ONE, str(destination))
    database.retire_provider_session("base")
    assert database.verify_retained_persona_claim(UUID_ONE, str(destination)) == 0
    alias = database.register_provider_session(
        **_values("alias-after-fence", terminal_id="terminal-alias")
    )
    assert alias["retained_persona_home"] is None


def test_t2c_n_alias_after_zero_owner_check_is_born_unclaimed(
    retention_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.register_provider_session(**_values("base"))
    source = retention_env / "source-codex-home"
    source.mkdir()
    destination = retention_env / "cao-personas" / "retained" / UUID_ONE
    destination.parent.mkdir(parents=True)
    destination.parent.chmod(0o700)
    monkeypatch.setattr(
        persona_context,
        "load_persona_plan",
        lambda terminal_id: SimpleNamespace(provider="codex", codex_home=source),
    )
    real_verify = database.verify_retained_persona_claim
    real_rmtree = persona_context.shutil.rmtree
    alias_claims: list[str | None] = []

    def zero_ready_owner(session_uuid: str, path: str) -> int:
        with database.SessionLocal() as db:
            row = db.query(database.ProviderSessionModel).filter_by(name="base").one()
            row.status = "superseded"
            db.commit()
        assert real_verify(session_uuid, path) == 0
        return 0

    def register_alias_before_compensation(path: Path, *args: object, **kwargs: object) -> None:
        alias = database.register_provider_session(
            **_values("alias-during-compensation", terminal_id="terminal-alias")
        )
        alias_claims.append(alias["retained_persona_home"])
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(database, "verify_retained_persona_claim", zero_ready_owner)
    monkeypatch.setattr(persona_context.shutil, "rmtree", register_alias_before_compensation)

    assert (
        persona_context.retain_codex_persona_home(
            "terminal-one", persona_context.PersonaRetentionIntent(UUID_ONE, destination, (1,))
        )
        is None
    )
    assert alias_claims == [None]
    assert not destination.exists()
    assert database.list_retained_persona_claims() == []


def test_startup_sweep_clears_dangling_claim_and_orphan_directory(
    retention_env: Path,
) -> None:
    database.register_provider_session(**_values("base"))
    retained = retention_env / "cao-personas" / "retained"
    missing = retained / UUID_ONE
    orphan = retained / UUID_TWO
    orphan.mkdir(parents=True)
    database.claim_retained_persona_home(UUID_ONE, str(missing))
    reconcile_retained_persona_homes()
    assert database.get_ready_provider_session("base")["retained_persona_home"] is None
    assert not orphan.exists()


def test_sandbox_startup_skips_production_retained_sweep(
    retention_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retained = retention_env / "cao-personas" / "retained" / UUID_ONE
    retained.mkdir(parents=True)
    monkeypatch.setenv("CAO_INSTANCE_ID", "sandbox-test")
    reconcile_retained_persona_homes()
    assert retained.is_dir()


def test_concurrent_retire_and_supersession_leave_no_dangling_claim(
    retention_env: Path,
) -> None:
    database.register_provider_session(**_values("base"))
    destination = retention_env / "cao-personas" / "retained" / UUID_ONE
    destination.mkdir(parents=True)
    database.claim_retained_persona_home(UUID_ONE, str(destination))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(database.retire_provider_session, "base"),
            pool.submit(
                database.register_provider_session,
                **_values("base", session_uuid=UUID_TWO, terminal_id="terminal-two"),
            ),
        ]
        for future in futures:
            future.result()

    claims = database.list_retained_persona_claims()
    assert all(Path(row["retained_persona_home"]).is_dir() for row in claims)
    assert all(
        row.retained_persona_home is None or Path(row.retained_persona_home).is_dir()
        for row in [*_rows(UUID_ONE), *_rows(UUID_TWO)]
    )
