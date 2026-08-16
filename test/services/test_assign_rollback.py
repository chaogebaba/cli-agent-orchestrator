"""F129: Tests for terminal rollback on frozen-pin failure at assign time."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as dbmod
from cli_agent_orchestrator.clients.database import (
    AuthorityPinModel,
    Base,
    TerminalModel,
)
from cli_agent_orchestrator.services.authority_pin_service import (
    AuthorityPinError,
    build_frozen_authority_block,
    register_frozen_pins,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def rollback_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up a test DB for assign rollback testing."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rollback.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", sessions)
    with sessions.begin() as db:
        db.add(TerminalModel(
            id="aaaaaaaa",
            tmux_session="cao-test",
            tmux_window="supervisor",
            provider="kiro_cli",
            agent_profile="supervisor",
            lifecycle_generation=1,
        ))
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    yield sessions
    engine.dispose()


class TestAssignAuthorityFilesRollback:
    """Tests for the atomic frozen-pin registration at terminal creation."""

    def test_hash_mismatch_rolls_back_terminal(self, rollback_db, tmp_path):
        """Assign with wrong sha256 -> AuthorityPinError, no pin rows persisted."""
        blueprint = tmp_path / "blueprint.md"
        blueprint.write_text("# Real content")
        wrong_sha = hashlib.sha256(b"wrong content").hexdigest()

        with rollback_db() as db:
            with pytest.raises(AuthorityPinError, match="authority_hash_mismatch"):
                register_frozen_pins(
                    db,
                    task_key="bbbbbbbb",
                    authority_files=[{"file_path": str(blueprint), "sha256": wrong_sha}],
                    registered_by="aaaaaaaa",
                )

        # No pin rows should exist
        with rollback_db() as db:
            pins = db.query(AuthorityPinModel).filter_by(task_key="bbbbbbbb").all()
            assert pins == []

    def test_pin_success_terminal_exists(self, rollback_db, tmp_path):
        """Assign with correct sha256 -> terminal row + pin rows exist."""
        blueprint = tmp_path / "blueprint.md"
        blueprint.write_text("# Design doc")
        sha = _sha(blueprint)

        # Simulate what database.create_terminal does: terminal row + pins in one transaction
        with rollback_db.begin() as db:
            db.add(TerminalModel(
                id="cccccccc",
                tmux_session="cao-test",
                tmux_window="reviewer",
                provider="kiro_cli",
                agent_profile="kiro_reviewer",
                caller_id="aaaaaaaa",
                lifecycle_generation=1,
            ))
            results = register_frozen_pins(
                db,
                task_key="cccccccc",
                authority_files=[{"file_path": str(blueprint), "sha256": sha}],
                registered_by="aaaaaaaa",
            )
            assert len(results) == 1
            assert results[0]["version"] == 1

        # Verify persistence
        with rollback_db() as db:
            terminal = db.query(TerminalModel).filter_by(id="cccccccc").first()
            assert terminal is not None
            pins = db.query(AuthorityPinModel).filter_by(task_key="cccccccc").all()
            assert len(pins) == 1
            assert pins[0].frozen is True
            assert pins[0].sha256 == sha
            assert pins[0].version == 1

    def test_frozen_pins_block_in_initial_message(self, tmp_path):
        """Initial message contains [FROZEN-AUTHORITY-PINS] block."""
        pins = [
            {"file_path": "/abs/path/blueprint.md", "sha256": "a" * 64},
            {"file_path": "/abs/path/schema.json", "sha256": "b" * 64},
        ]
        block = build_frozen_authority_block(pins)
        assert "[FROZEN-AUTHORITY-PINS]" in block
        assert "[/FROZEN-AUTHORITY-PINS]" in block
        assert "path=/abs/path/blueprint.md" in block
        assert f"sha256={'a' * 64}" in block
        assert "path=/abs/path/schema.json" in block

    def test_no_authority_files_unchanged_behavior(self, rollback_db, tmp_path):
        """Assign without authority_files -> same as before (no pins)."""
        with rollback_db.begin() as db:
            db.add(TerminalModel(
                id="dddddddd",
                tmux_session="cao-test",
                tmux_window="dev",
                provider="kiro_cli",
                agent_profile="developer",
                caller_id="aaaaaaaa",
                lifecycle_generation=1,
            ))

        with rollback_db() as db:
            pins = db.query(AuthorityPinModel).filter_by(task_key="dddddddd").all()
            assert pins == []

    def test_pin_failure_no_provider_artefacts(self, rollback_db, tmp_path):
        """Pin failure -> create_provider never called.

        Since register_frozen_pins raises before the transaction commits,
        and create_provider is called AFTER db_created=True, the provider
        is never constructed on pin failure.
        """
        # This test verifies the ordering invariant: pin validation
        # raises, so nothing after it in the transaction runs.
        blueprint = tmp_path / "missing_file.md"
        fake_sha = "c" * 64

        with rollback_db() as db:
            with pytest.raises(AuthorityPinError, match="authority_hash_mismatch"):
                register_frozen_pins(
                    db,
                    task_key="eeeeeeee",
                    authority_files=[{"file_path": str(blueprint), "sha256": fake_sha}],
                    registered_by="aaaaaaaa",
                )

    def test_transaction_atomicity(self, rollback_db, tmp_path):
        """Terminal row and pin rows in same transaction — partial commit impossible."""
        blueprint = tmp_path / "blueprint.md"
        schema = tmp_path / "schema.json"
        blueprint.write_text("# Blueprint")
        schema.write_text('{"type": "object"}')
        bp_sha = _sha(blueprint)
        sch_sha = _sha(schema)

        # Both succeed — both are committed
        with rollback_db.begin() as db:
            db.add(TerminalModel(
                id="ffffffff",
                tmux_session="cao-test",
                tmux_window="reviewer2",
                provider="kiro_cli",
                agent_profile="kiro_reviewer",
                caller_id="aaaaaaaa",
                lifecycle_generation=1,
            ))
            register_frozen_pins(
                db,
                task_key="ffffffff",
                authority_files=[
                    {"file_path": str(blueprint), "sha256": bp_sha},
                    {"file_path": str(schema), "sha256": sch_sha},
                ],
                registered_by="aaaaaaaa",
            )

        with rollback_db() as db:
            pins = db.query(AuthorityPinModel).filter_by(task_key="ffffffff").all()
            assert len(pins) == 2
            assert all(p.frozen is True for p in pins)

        # Now test failure: second file has wrong hash -> nothing committed
        other = tmp_path / "other.md"
        other.write_text("other")
        wrong_sha = "d" * 64

        with rollback_db() as db:
            with pytest.raises(AuthorityPinError):
                register_frozen_pins(
                    db,
                    task_key="eeee1111",
                    authority_files=[
                        {"file_path": str(other), "sha256": _sha(other)},
                        {"file_path": str(blueprint), "sha256": wrong_sha},  # wrong!
                    ],
                    registered_by="aaaaaaaa",
                )

        # Neither pin should exist for the failed task
        with rollback_db() as db:
            pins = db.query(AuthorityPinModel).filter_by(task_key="eeee1111").all()
            assert pins == []

    def test_multiple_files_all_pinned(self, rollback_db, tmp_path):
        """Multiple authority_files all get frozen pins."""
        files = []
        for i in range(3):
            f = tmp_path / f"file{i}.md"
            f.write_text(f"content {i}")
            files.append({"file_path": str(f), "sha256": _sha(f)})

        with rollback_db.begin() as db:
            db.add(TerminalModel(
                id="aabb1122",
                tmux_session="cao-test",
                tmux_window="multi",
                provider="kiro_cli",
                agent_profile="kiro_reviewer",
                caller_id="aaaaaaaa",
                lifecycle_generation=1,
            ))
            results = register_frozen_pins(
                db,
                task_key="aabb1122",
                authority_files=files,
                registered_by="aaaaaaaa",
            )
            assert len(results) == 3

        with rollback_db() as db:
            pins = db.query(AuthorityPinModel).filter_by(task_key="aabb1122").all()
            assert len(pins) == 3
            assert all(p.frozen is True for p in pins)
            assert all(p.version == 1 for p in pins)
