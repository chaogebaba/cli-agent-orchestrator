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


@pytest.mark.slow  # F254 D19: exceeds unit budget
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



# ─── F129: Frozen pin tests ─────────────────────────────────────────────────


class TestRegisterFrozenPins:
    """Tests for register_frozen_pins (atomic frozen pin registration)."""

    def test_registers_version1_frozen_true(self, pin_db, tmp_path):
        """Frozen pins have frozen=True, version=1."""
        authority = tmp_path / "blueprint.md"
        authority.write_text("content")
        sha = _sha(authority)

        from cli_agent_orchestrator.services.authority_pin_service import register_frozen_pins

        with pin_db.begin() as db:
            results = register_frozen_pins(
                db,
                task_key="bbbbbbbb",
                authority_files=[{"file_path": str(authority), "sha256": sha}],
                registered_by="aaaaaaaa",
            )
            assert len(results) == 1
            assert results[0]["version"] == 1
            assert results[0]["sha256"] == sha

        with pin_db() as db:
            row = (
                db.query(dbmod.AuthorityPinModel)
                .filter_by(task_key="bbbbbbbb", file_path=str(authority))
                .first()
            )
            assert row is not None
            assert row.frozen is True
            assert row.version == 1

    def test_hash_mismatch_raises_authority_hash_mismatch(self, pin_db, tmp_path):
        """Caller sha256 != server-computed -> AuthorityPinError."""
        authority = tmp_path / "blueprint.md"
        authority.write_text("real content")
        wrong_sha = hashlib.sha256(b"wrong").hexdigest()

        from cli_agent_orchestrator.services.authority_pin_service import register_frozen_pins

        with pin_db() as db:
            with pytest.raises(service.AuthorityPinError, match="authority_hash_mismatch"):
                register_frozen_pins(
                    db,
                    task_key="bbbbbbbb",
                    authority_files=[{"file_path": str(authority), "sha256": wrong_sha}],
                    registered_by="aaaaaaaa",
                )

    def test_path_not_absolute_rejected(self, pin_db, tmp_path):
        """Relative path -> AuthorityPinError('path_not_absolute')."""
        from cli_agent_orchestrator.services.authority_pin_service import register_frozen_pins

        with pin_db() as db:
            with pytest.raises(service.AuthorityPinError, match="path_not_absolute"):
                register_frozen_pins(
                    db,
                    task_key="bbbbbbbb",
                    authority_files=[{"file_path": "relative.md", "sha256": "a" * 64}],
                    registered_by="aaaaaaaa",
                )

    def test_empty_list_rejected(self, pin_db, tmp_path):
        """Empty authority_files -> AuthorityPinError('empty_pin_list')."""
        from cli_agent_orchestrator.services.authority_pin_service import register_frozen_pins

        with pin_db() as db:
            with pytest.raises(service.AuthorityPinError, match="empty_pin_list"):
                register_frozen_pins(
                    db,
                    task_key="bbbbbbbb",
                    authority_files=[],
                    registered_by="aaaaaaaa",
                )

    def test_duplicate_paths_rejected(self, pin_db, tmp_path):
        """Duplicate file_path in list -> AuthorityPinError('duplicate_path')."""
        authority = tmp_path / "blueprint.md"
        authority.write_text("content")
        sha = _sha(authority)

        from cli_agent_orchestrator.services.authority_pin_service import register_frozen_pins

        with pin_db() as db:
            with pytest.raises(service.AuthorityPinError, match="duplicate_path"):
                register_frozen_pins(
                    db,
                    task_key="bbbbbbbb",
                    authority_files=[
                        {"file_path": str(authority), "sha256": sha},
                        {"file_path": str(authority), "sha256": sha},
                    ],
                    registered_by="aaaaaaaa",
                )

    def test_transaction_atomicity_with_terminal_row(self, pin_db, tmp_path):
        """Pin rows and terminal row committed in ONE transaction."""
        authority = tmp_path / "blueprint.md"
        authority.write_text("content")
        sha = _sha(authority)

        from cli_agent_orchestrator.services.authority_pin_service import register_frozen_pins

        # Simulate atomic: create terminal + pins in one begin block
        with pin_db.begin() as db:
            db.add(TerminalModel(
                id="dddddddd",
                tmux_session="cao-test",
                tmux_window="new-worker",
                provider="codex",
                agent_profile="developer",
                caller_id="aaaaaaaa",
                lifecycle_generation=1,
            ))
            register_frozen_pins(
                db,
                task_key="dddddddd",
                authority_files=[{"file_path": str(authority), "sha256": sha}],
                registered_by="aaaaaaaa",
            )

        # Both committed
        with pin_db() as db:
            assert db.query(TerminalModel).filter_by(id="dddddddd").first() is not None
            pins = db.query(dbmod.AuthorityPinModel).filter_by(task_key="dddddddd").all()
            assert len(pins) == 1


class TestValidateFrozenPins:
    """Tests for validate_frozen_pins."""

    def test_no_frozen_pins_returns_no_frozen_pins(self, pin_db, tmp_path):
        """Worker with no frozen rows -> outcome='no_frozen_pins'."""
        from cli_agent_orchestrator.services.authority_pin_service import validate_frozen_pins

        with pin_db() as db:
            result = validate_frozen_pins(db, "cccccccc")
            assert result.outcome == "no_frozen_pins"

    def test_all_valid_returns_valid(self, pin_db, tmp_path):
        """All hashes match -> outcome='valid'."""
        authority = tmp_path / "blueprint.md"
        authority.write_text("stable content")
        sha = _sha(authority)

        with pin_db.begin() as db:
            db.add(dbmod.AuthorityPinModel(
                task_key="bbbbbbbb",
                file_path=str(authority),
                sha256=sha,
                version=1,
                registered_by="aaaaaaaa",
                frozen=True,
            ))

        from cli_agent_orchestrator.services.authority_pin_service import validate_frozen_pins

        with pin_db() as db:
            result = validate_frozen_pins(db, "bbbbbbbb")
            assert result.outcome == "valid"
            assert len(result.all_results) == 1
            assert result.all_results[0].verdict == "VALID"

    def test_one_drift_returns_drift(self, pin_db, tmp_path):
        """One file modified -> outcome='drift'."""
        authority = tmp_path / "blueprint.md"
        authority.write_text("original")
        sha = _sha(authority)

        with pin_db.begin() as db:
            db.add(dbmod.AuthorityPinModel(
                task_key="bbbbbbbb",
                file_path=str(authority),
                sha256=sha,
                version=1,
                registered_by="aaaaaaaa",
                frozen=True,
            ))

        authority.write_text("modified")

        from cli_agent_orchestrator.services.authority_pin_service import validate_frozen_pins

        with pin_db() as db:
            result = validate_frozen_pins(db, "bbbbbbbb")
            assert result.outcome == "drift"
            assert len(result.drifted) == 1
            assert result.drifted[0].reason == "content"

    def test_missing_file_returns_drift_reason_missing(self, pin_db, tmp_path):
        """File deleted -> DRIFT with reason='missing'."""
        fake_path = str(tmp_path / "nonexistent.md")

        with pin_db.begin() as db:
            db.add(dbmod.AuthorityPinModel(
                task_key="bbbbbbbb",
                file_path=fake_path,
                sha256="a" * 64,
                version=1,
                registered_by="aaaaaaaa",
                frozen=True,
            ))

        from cli_agent_orchestrator.services.authority_pin_service import validate_frozen_pins

        with pin_db() as db:
            result = validate_frozen_pins(db, "bbbbbbbb")
            assert result.outcome == "drift"
            assert result.drifted[0].reason == "missing"

    def test_ignores_mutable_pins(self, pin_db, tmp_path):
        """Only frozen=True rows are validated; frozen=False skipped."""
        authority = tmp_path / "mutable.md"
        authority.write_text("content")
        sha = _sha(authority)

        with pin_db.begin() as db:
            db.add(dbmod.AuthorityPinModel(
                task_key="bbbbbbbb",
                file_path=str(authority),
                sha256=sha,
                version=1,
                registered_by="aaaaaaaa",
                frozen=False,  # mutable
            ))

        from cli_agent_orchestrator.services.authority_pin_service import validate_frozen_pins

        with pin_db() as db:
            result = validate_frozen_pins(db, "bbbbbbbb")
            assert result.outcome == "no_frozen_pins"


class TestUpdatePinFrozenGuard:
    """Tests for frozen pin immutability guard on update_pin."""

    def test_update_pin_on_frozen_raises(self, pin_db, tmp_path):
        """update_pin on a frozen pin -> AuthorityPinError('frozen_pin_immutable')."""
        authority = tmp_path / "frozen.md"
        authority.write_text("frozen content")
        sha = _sha(authority)

        with pin_db.begin() as db:
            db.add(dbmod.AuthorityPinModel(
                task_key="bbbbbbbb",
                file_path=str(authority),
                sha256=sha,
                version=1,
                registered_by="aaaaaaaa",
                frozen=True,
            ))

        new_sha = hashlib.sha256(b"new").hexdigest()
        with pytest.raises(service.AuthorityPinError, match="frozen_pin_immutable"):
            service.update_pin("bbbbbbbb", str(authority), new_sha)

    def test_update_pin_on_mutable_succeeds(self, pin_db, tmp_path):
        """update_pin on a mutable pin -> works as before."""
        authority = tmp_path / "mutable.md"
        authority.write_text("mutable content")
        sha = _sha(authority)

        service.pin_authority("bbbbbbbb", [{"file_path": str(authority), "sha256": sha}])

        new_sha = hashlib.sha256(b"v2").hexdigest()
        result = service.update_pin("bbbbbbbb", str(authority), new_sha)
        assert result["current_version"] == 2



# ─── F495: Frozen-pin rotation tests ────────────────────────────────────────


class TestRotateFrozenPins:
    """Tests for rotate_frozen_pins (F495: warm-reuse pin rotation)."""

    def test_rotation_replaces_old_pins_with_new(self, pin_db, tmp_path):
        """rotate_frozen_pins deletes old frozen pins and registers new ones."""
        old_authority = tmp_path / "old-blueprint.md"
        old_authority.write_text("old content")
        old_sha = _sha(old_authority)

        new_authority = tmp_path / "new-blueprint.md"
        new_authority.write_text("new content")
        new_sha = _sha(new_authority)

        from cli_agent_orchestrator.services.authority_pin_service import (
            register_frozen_pins,
            rotate_frozen_pins,
        )

        # First dispatch: register old pin
        with pin_db.begin() as db:
            register_frozen_pins(
                db,
                task_key="bbbbbbbb",
                authority_files=[{"file_path": str(old_authority), "sha256": old_sha}],
                registered_by="aaaaaaaa",
            )

        # Second dispatch (warm-reuse): rotate to new pin
        with pin_db.begin() as db:
            results = rotate_frozen_pins(
                db,
                task_key="bbbbbbbb",
                authority_files=[{"file_path": str(new_authority), "sha256": new_sha}],
                registered_by="aaaaaaaa",
            )
            assert len(results) == 1
            assert results[0]["file_path"] == str(new_authority)
            assert results[0]["sha256"] == new_sha
            assert results[0]["version"] == 1

        # Verify old pin is gone, new pin exists
        with pin_db() as db:
            old_rows = (
                db.query(dbmod.AuthorityPinModel)
                .filter_by(task_key="bbbbbbbb", file_path=str(old_authority))
                .all()
            )
            assert old_rows == []
            new_rows = (
                db.query(dbmod.AuthorityPinModel)
                .filter_by(task_key="bbbbbbbb", file_path=str(new_authority))
                .all()
            )
            assert len(new_rows) == 1
            assert new_rows[0].frozen is True
            assert new_rows[0].version == 1
            assert new_rows[0].sha256 == new_sha

    def test_rotation_preserves_mutable_pins(self, pin_db, tmp_path):
        """rotate_frozen_pins only deletes frozen=True rows, not mutable ones."""
        frozen_file = tmp_path / "frozen.md"
        frozen_file.write_text("frozen")
        frozen_sha = _sha(frozen_file)

        mutable_file = tmp_path / "mutable.md"
        mutable_file.write_text("mutable")
        mutable_sha = _sha(mutable_file)

        new_file = tmp_path / "new.md"
        new_file.write_text("new")
        new_sha = _sha(new_file)

        from cli_agent_orchestrator.services.authority_pin_service import (
            register_frozen_pins,
            rotate_frozen_pins,
        )

        # Register one frozen pin and one mutable pin
        with pin_db.begin() as db:
            register_frozen_pins(
                db,
                task_key="bbbbbbbb",
                authority_files=[{"file_path": str(frozen_file), "sha256": frozen_sha}],
                registered_by="aaaaaaaa",
            )
        service.pin_authority("bbbbbbbb", [{"file_path": str(mutable_file), "sha256": mutable_sha}])

        # Rotate: should only delete the frozen pin
        with pin_db.begin() as db:
            rotate_frozen_pins(
                db,
                task_key="bbbbbbbb",
                authority_files=[{"file_path": str(new_file), "sha256": new_sha}],
                registered_by="aaaaaaaa",
            )

        with pin_db() as db:
            # Mutable pin still exists
            mutable_rows = (
                db.query(dbmod.AuthorityPinModel)
                .filter_by(task_key="bbbbbbbb", file_path=str(mutable_file))
                .all()
            )
            assert len(mutable_rows) == 1
            assert mutable_rows[0].frozen is False

            # Old frozen pin gone
            old_frozen = (
                db.query(dbmod.AuthorityPinModel)
                .filter_by(task_key="bbbbbbbb", file_path=str(frozen_file))
                .all()
            )
            assert old_frozen == []

            # New frozen pin exists
            new_frozen = (
                db.query(dbmod.AuthorityPinModel)
                .filter_by(task_key="bbbbbbbb", file_path=str(new_file))
                .all()
            )
            assert len(new_frozen) == 1
            assert new_frozen[0].frozen is True

    def test_rotation_validates_hash(self, pin_db, tmp_path):
        """rotate_frozen_pins rejects mismatched hashes like register does."""
        old_file = tmp_path / "old.md"
        old_file.write_text("old")
        old_sha = _sha(old_file)

        new_file = tmp_path / "new.md"
        new_file.write_text("new content")
        wrong_sha = hashlib.sha256(b"wrong").hexdigest()

        from cli_agent_orchestrator.services.authority_pin_service import (
            register_frozen_pins,
            rotate_frozen_pins,
        )

        with pin_db.begin() as db:
            register_frozen_pins(
                db,
                task_key="bbbbbbbb",
                authority_files=[{"file_path": str(old_file), "sha256": old_sha}],
                registered_by="aaaaaaaa",
            )

        with pin_db() as db:
            with pytest.raises(service.AuthorityPinError, match="authority_hash_mismatch"):
                rotate_frozen_pins(
                    db,
                    task_key="bbbbbbbb",
                    authority_files=[{"file_path": str(new_file), "sha256": wrong_sha}],
                    registered_by="aaaaaaaa",
                )

    def test_rotation_same_file_different_content(self, pin_db, tmp_path):
        """Rotation on the same file path with updated content succeeds."""
        authority = tmp_path / "blueprint.md"
        authority.write_text("v1 content")
        v1_sha = _sha(authority)

        from cli_agent_orchestrator.services.authority_pin_service import (
            register_frozen_pins,
            rotate_frozen_pins,
        )

        with pin_db.begin() as db:
            register_frozen_pins(
                db,
                task_key="bbbbbbbb",
                authority_files=[{"file_path": str(authority), "sha256": v1_sha}],
                registered_by="aaaaaaaa",
            )

        # File is rewritten (r2->r3 scenario)
        authority.write_text("v2 content")
        v2_sha = _sha(authority)

        with pin_db.begin() as db:
            results = rotate_frozen_pins(
                db,
                task_key="bbbbbbbb",
                authority_files=[{"file_path": str(authority), "sha256": v2_sha}],
                registered_by="aaaaaaaa",
            )
            assert results[0]["sha256"] == v2_sha

        # Validate: should see VALID (not DRIFT)
        from cli_agent_orchestrator.services.authority_pin_service import validate_frozen_pins

        with pin_db() as db:
            validation = validate_frozen_pins(db, "bbbbbbbb")
            assert validation.outcome == "valid"
            assert validation.all_results[0].verdict == "VALID"


class TestF495RegressionSuppressedValidVerdict:
    """Regression test for the exact live incident: warm reviewer's valid
    verdict suppressed as FROZEN-PIN-DRIFT because old pin cited stale hash.

    Scenario:
      1. Reviewer terminal R dispatched with authority_files=[blueprint v1]
      2. Builder overwrites blueprint to v2 (legitimate r2->r3)
      3. Supervisor re-dispatches R with authority_files=[blueprint v2]
      4. R completes review, sends callback
      5. validate_frozen_pins should see VALID (blueprint matches v2 pin)
      6. Before fix: validate saw DRIFT (old v1 pin vs v2 file) → callback suppressed
    """

    def test_warm_reviewer_re_pinned_attests_current_artifact(self, pin_db, tmp_path):
        """After rotation, validate_frozen_pins attests the CURRENT artifact."""
        blueprint = tmp_path / "f158-build-report.md"
        blueprint.write_text("round 2 report content")
        r2_sha = _sha(blueprint)

        from cli_agent_orchestrator.services.authority_pin_service import (
            register_frozen_pins,
            rotate_frozen_pins,
            validate_frozen_pins,
            build_attestation,
        )

        # Step 1: First dispatch — pin to r2 content
        with pin_db.begin() as db:
            register_frozen_pins(
                db,
                task_key="bbbbbbbb",
                authority_files=[{"file_path": str(blueprint), "sha256": r2_sha}],
                registered_by="aaaaaaaa",
            )

        # Step 2: Builder rewrites blueprint for r3
        blueprint.write_text("round 3 report content")
        r3_sha = _sha(blueprint)

        # Verify: OLD pin would trigger drift (the bug)
        with pin_db() as db:
            old_validation = validate_frozen_pins(db, "bbbbbbbb")
            assert old_validation.outcome == "drift"  # This was the bug

        # Step 3: Supervisor re-dispatches with new authority_files (the fix)
        with pin_db.begin() as db:
            rotate_frozen_pins(
                db,
                task_key="bbbbbbbb",
                authority_files=[{"file_path": str(blueprint), "sha256": r3_sha}],
                registered_by="aaaaaaaa",
            )

        # Step 4: Reviewer sends callback — validation should PASS
        with pin_db() as db:
            new_validation = validate_frozen_pins(db, "bbbbbbbb")
            assert new_validation.outcome == "valid"
            assert new_validation.all_results[0].verdict == "VALID"
            assert new_validation.all_results[0].expected == r3_sha

            # Attestation references the CURRENT (r3) hash
            attestation = build_attestation(new_validation)
            assert r3_sha in attestation
            assert r2_sha not in attestation

    def test_genuine_drift_still_detected_after_rotation(self, pin_db, tmp_path):
        """Post-dispatch mutation of pinned file still triggers DRIFT."""
        blueprint = tmp_path / "f337-build-report.md"
        blueprint.write_text("dispatched content")
        dispatch_sha = _sha(blueprint)

        from cli_agent_orchestrator.services.authority_pin_service import (
            rotate_frozen_pins,
            register_frozen_pins,
            validate_frozen_pins,
        )

        # First dispatch
        with pin_db.begin() as db:
            register_frozen_pins(
                db,
                task_key="bbbbbbbb",
                authority_files=[{"file_path": str(blueprint), "sha256": dispatch_sha}],
                registered_by="aaaaaaaa",
            )

        # Rotate to new pin (simulating warm-reuse re-dispatch)
        blueprint.write_text("re-dispatched content")
        redispatch_sha = _sha(blueprint)
        with pin_db.begin() as db:
            rotate_frozen_pins(
                db,
                task_key="bbbbbbbb",
                authority_files=[{"file_path": str(blueprint), "sha256": redispatch_sha}],
                registered_by="aaaaaaaa",
            )

        # GENUINE post-dispatch mutation (not a legitimate re-dispatch)
        blueprint.write_text("tampered content")

        with pin_db() as db:
            validation = validate_frozen_pins(db, "bbbbbbbb")
            assert validation.outcome == "drift"
            assert validation.drifted[0].reason == "content"
            assert validation.drifted[0].expected == redispatch_sha
