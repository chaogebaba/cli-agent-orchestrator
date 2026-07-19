"""Behavioral controls for the WPQ13 authority-pin registry."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as dbmod
from cli_agent_orchestrator.clients.database import Base, TerminalModel
from cli_agent_orchestrator.services import authority_pin_service as service


@pytest.fixture
def pin_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'authority-pin.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", sessions)
    with sessions.begin() as db:
        db.add_all(
            [
                TerminalModel(
                    id="aaaaaaaa",
                    tmux_session="cao-test",
                    tmux_window="owner",
                    provider="codex",
                    agent_profile="supervisor",
                    lifecycle_generation=1,
                ),
                TerminalModel(
                    id="bbbbbbbb",
                    tmux_session="cao-test",
                    tmux_window="worker",
                    provider="codex",
                    agent_profile="developer",
                    caller_id="aaaaaaaa",
                    lifecycle_generation=1,
                ),
                TerminalModel(
                    id="cccccccc",
                    tmux_session="cao-test",
                    tmux_window="other",
                    provider="codex",
                    agent_profile="developer",
                    caller_id="aaaaaaaa",
                    lifecycle_generation=1,
                ),
            ]
        )
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    yield sessions
    engine.dispose()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_register_verify_update_equal_sha_precedence_and_persistence(pin_db, tmp_path):
    authority = tmp_path / "authority.md"
    authority.write_text("alpha")
    first = _sha(authority)
    authority.write_text("beta")
    second = _sha(authority)
    authority.write_text("alpha")

    result = service.pin_authority("bbbbbbbb", [{"file_path": str(authority), "sha256": first}])
    assert result["task_key"] == "bbbbbbbb"
    assert result["results"][0]["current_version"] == 1
    assert service.verify_pin(str(authority)) == {"verdict": "UNPINNED"}
    pin_db  # keep the fixture's session factory in the test's named contract
    # Verification is scoped to the worker terminal, not the supervisor.
    os.environ["CAO_TERMINAL_ID"] = "bbbbbbbb"
    assert service.verify_pin(str(authority)) == {"verdict": "VALID", "version": 1}
    os.environ["CAO_TERMINAL_ID"] = "aaaaaaaa"
    assert service.update_pin("bbbbbbbb", str(authority), second)["current_version"] == 2
    assert service.update_pin("bbbbbbbb", str(authority), first)["current_version"] == 3
    os.environ["CAO_TERMINAL_ID"] = "bbbbbbbb"
    verdict = service.verify_pin(str(authority))
    assert verdict["verdict"] == "SUPERSEDED"
    assert verdict["current_version"] == 3
    assert [entry["version"] for entry in verdict["chain"]] == [1, 2, 3]
    assert verdict["current_sha"] == first


def test_drift_truth_table_and_start_race_visibility(pin_db, tmp_path):
    authority = tmp_path / "authority.md"
    authority.write_text("v1")
    first = _sha(authority)
    os.environ["CAO_TERMINAL_ID"] = "cccccccc"
    assert service.verify_pin(str(authority)) == {"verdict": "UNPINNED"}

    os.environ["CAO_TERMINAL_ID"] = "aaaaaaaa"
    service.pin_authority("cccccccc", [{"file_path": str(authority), "sha256": first}])
    os.environ["CAO_TERMINAL_ID"] = "cccccccc"
    assert service.verify_pin(str(authority)) == {"verdict": "VALID", "version": 1}
    authority.write_text("v2")
    second = _sha(authority)
    os.environ["CAO_TERMINAL_ID"] = "aaaaaaaa"
    service.update_pin("cccccccc", str(authority), second)
    os.environ["CAO_TERMINAL_ID"] = "cccccccc"
    authority.write_text("v1")
    stale = service.verify_pin(str(authority))
    assert stale["verdict"] == "DRIFT"
    assert stale["reason"] == "content"
    authority.write_text("unknown")
    unknown = service.verify_pin(str(authority))
    assert unknown["verdict"] == "DRIFT"
    assert unknown["reason"] == "content"
    assert service.verify_pin(str(authority)) == unknown


def test_principal_is_captured_and_persisted(pin_db, tmp_path):
    authority = tmp_path / "authority.md"
    authority.write_text("authority")
    sha = _sha(authority)
    with pytest.raises(TypeError):
        service.pin_authority(
            "bbbbbbbb", [{"file_path": str(authority), "sha256": sha}], principal="cccccccc"
        )
    service.pin_authority("bbbbbbbb", [{"file_path": str(authority), "sha256": sha}])
    with pin_db() as db:
        row = db.query(dbmod.AuthorityPinModel).one()
        assert row.registered_by == "aaaaaaaa"


def test_unreadable_path_and_lifecycle_rebind_fallback(pin_db, tmp_path):
    authority = tmp_path / "authority.md"
    authority.write_text("authority")
    sha = _sha(authority)
    service.pin_authority("bbbbbbbb", [{"file_path": str(authority), "sha256": sha}])
    authority.chmod(0)
    try:
        os.environ["CAO_TERMINAL_ID"] = "bbbbbbbb"
        assert service.verify_pin(str(authority))["reason"] == "unreadable"
    finally:
        authority.chmod(0o644)

    with pin_db.begin() as db:
        worker = db.query(TerminalModel).filter_by(id="bbbbbbbb").one()
        db.delete(worker)
    with pin_db.begin() as db:
        db.add(
            TerminalModel(
                id="bbbbbbbb",
                tmux_session="cao-test",
                tmux_window="recovered",
                provider="codex",
                agent_profile="developer",
                caller_id="aaaaaaaa",
                lifecycle_generation=2,
            )
        )
    assert service.verify_pin(str(authority)) == {"verdict": "VALID", "version": 1}
    os.environ["CAO_TERMINAL_ID"] = "cccccccc"
    assert service.verify_pin(str(authority)) == {"verdict": "UNPINNED"}


def test_pin_persists_after_database_reopen(pin_db, tmp_path, monkeypatch):
    authority = tmp_path / "authority.md"
    authority.write_text("authority")
    sha = _sha(authority)
    service.pin_authority("bbbbbbbb", [{"file_path": str(authority), "sha256": sha}])
    engine = pin_db.kw["bind"]
    engine.dispose()
    reopened = create_engine(
        f"sqlite:///{tmp_path / 'authority-pin.db'}",
        connect_args={"check_same_thread": False},
    )
    reopened_sessions = sessionmaker(bind=reopened, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", reopened_sessions)
    monkeypatch.setenv("CAO_TERMINAL_ID", "bbbbbbbb")
    assert service.verify_pin(str(authority)) == {"verdict": "VALID", "version": 1}
    reopened.dispose()


def test_atomic_multi_pin_and_validation_errors(pin_db, tmp_path):
    one = tmp_path / "one.md"
    two = tmp_path / "two.md"
    one.write_text("one")
    two.write_text("two")
    one_sha, two_sha = _sha(one), _sha(two)
    service.pin_authority("bbbbbbbb", [{"file_path": str(one), "sha256": one_sha}])
    with pytest.raises(service.AuthorityPinError) as exc:
        service.pin_authority(
            "bbbbbbbb",
            [
                {"file_path": str(two), "sha256": two_sha},
                {"file_path": str(one), "sha256": one_sha},
            ],
        )
    assert exc.value.code == "already_pinned"
    with pin_db() as db:
        assert db.query(dbmod.AuthorityPinModel).filter_by(file_path=str(two)).count() == 0
    with pytest.raises(service.AuthorityPinError, match="duplicate_path"):
        service.pin_authority(
            "bbbbbbbb",
            [
                {"file_path": str(two), "sha256": two_sha},
                {"file_path": str(two), "sha256": two_sha},
            ],
        )
    with pytest.raises(service.AuthorityPinError, match="empty_pin_list"):
        service.pin_authority("bbbbbbbb", [])
    with pytest.raises(service.AuthorityPinError, match="unknown_worker"):
        service.pin_authority("dddddddd", [{"file_path": str(two), "sha256": two_sha}])
    with pytest.raises(service.AuthorityPinError, match="path_not_absolute"):
        service.pin_authority("bbbbbbbb", [{"file_path": "relative.md", "sha256": one_sha}])
    with pytest.raises(service.AuthorityPinError, match="invalid_sha256"):
        service.pin_authority("bbbbbbbb", [{"file_path": str(two), "sha256": "bad"}])


def test_multi_pin_success_preserves_input_order(pin_db, tmp_path):
    paths = [tmp_path / "third.md", tmp_path / "first.md", tmp_path / "second.md"]
    for path in paths:
        path.write_text(path.name)
    result = service.pin_authority(
        "bbbbbbbb",
        [{"file_path": str(path), "sha256": _sha(path)} for path in paths],
    )
    assert [entry["file_path"] for entry in result["results"]] == [str(path) for path in paths]
    assert [entry["current_version"] for entry in result["results"]] == [1, 1, 1]
    assert [entry["chain"][0]["version"] for entry in result["results"]] == [1, 1, 1]


def test_principal_and_missing_terminal_errors(pin_db, tmp_path, monkeypatch):
    authority = tmp_path / "authority.md"
    authority.write_text("content")
    sha = _sha(authority)
    monkeypatch.setenv("CAO_TERMINAL_ID", "cccccccc")
    with pytest.raises(service.AuthorityPinError, match="not_owner"):
        service.pin_authority("bbbbbbbb", [{"file_path": str(authority), "sha256": sha}])
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    service.pin_authority("bbbbbbbb", [{"file_path": str(authority), "sha256": sha}])
    monkeypatch.setenv("CAO_TERMINAL_ID", "cccccccc")
    with pytest.raises(service.AuthorityPinError, match="not_owner"):
        service.update_pin("bbbbbbbb", str(authority), hashlib.sha256(b"next").hexdigest())
    monkeypatch.delenv("CAO_TERMINAL_ID")
    with pytest.raises(service.AuthorityPinError, match="missing_terminal_id"):
        service.verify_pin(str(authority))


def test_filesystem_drift_reasons_and_full_symlink_chain(pin_db, tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_text("target")
    link_one = tmp_path / "link-one"
    link_two = tmp_path / "link-two"
    link_one.symlink_to(target)
    link_two.symlink_to(link_one)
    sha = _sha(target)
    service.pin_authority("bbbbbbbb", [{"file_path": str(link_two), "sha256": sha}])
    monkeypatch.setenv("CAO_TERMINAL_ID", "bbbbbbbb")
    assert service.verify_pin(str(link_two))["verdict"] == "VALID"
    target.write_text("changed")
    assert service.verify_pin(str(link_two))["reason"] == "content"
    target.unlink()
    assert service.verify_pin(str(link_two)) == {
        "verdict": "DRIFT",
        "expected_sha": sha,
        "observed_sha": None,
        "reason": "missing",
    }

    directory = tmp_path / "directory"
    directory.mkdir()
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    service.pin_authority("bbbbbbbb", [{"file_path": str(directory), "sha256": sha}])
    monkeypatch.setenv("CAO_TERMINAL_ID", "bbbbbbbb")
    assert service.verify_pin(str(directory))["reason"] == "not_regular"


def test_concurrent_updates_serialize_without_duplicate_versions(pin_db, tmp_path):
    authority = tmp_path / "authority.md"
    authority.write_text("v1")
    first = _sha(authority)
    service.pin_authority("bbbbbbbb", [{"file_path": str(authority), "sha256": first}])
    errors: list[Exception] = []

    def update(value: str) -> None:
        try:
            service.update_pin(
                "bbbbbbbb", str(authority), hashlib.sha256(value.encode()).hexdigest()
            )
        except Exception as exc:  # pragma: no cover - assertion below reports any race failure.
            errors.append(exc)

    threads = [threading.Thread(target=update, args=(f"v{index}",)) for index in (2, 3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with pin_db() as db:
        versions = [
            row.version
            for row in db.query(dbmod.AuthorityPinModel)
            .filter_by(task_key="bbbbbbbb", file_path=str(authority))
            .order_by(dbmod.AuthorityPinModel.version)
        ]
    assert versions == [1, 2, 3]


def test_busy_lock_returns_db_busy(pin_db, tmp_path):
    authority = tmp_path / "authority.md"
    authority.write_text("v1")
    sha = _sha(authority)
    service.pin_authority("bbbbbbbb", [{"file_path": str(authority), "sha256": sha}])
    lock = pin_db()
    lock.execute(text("PRAGMA busy_timeout=1000"))
    lock.execute(text("BEGIN IMMEDIATE"))
    try:
        started = time.monotonic()
        with pytest.raises(service.AuthorityPinError, match="db_busy"):
            service.update_pin("bbbbbbbb", str(authority), hashlib.sha256(b"v2").hexdigest())
        assert time.monotonic() - started >= 0.8
    finally:
        lock.rollback()
        lock.close()
