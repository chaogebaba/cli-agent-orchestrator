"""F295 Half 2 — System metadata (D12) tests (AC12, AC13)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, TerminalModel


def _make_engine_and_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _insert_terminal(session_cls, tid: str, provider: str, metadata_json: str | None = None):
    with session_cls() as db:
        t = TerminalModel()
        t.id = tid
        t.tmux_session = "s1"
        t.tmux_window = f"w-{tid}"
        t.provider = provider
        t.metadata_json = metadata_json
        db.add(t)
        db.commit()


# ---------------------------------------------------------------------------
# AC12: worker full-replace cannot erase a system flag
# ---------------------------------------------------------------------------


class TestSystemMetadataProtection:
    """D12: the reserved 'cao' namespace survives worker full-replace."""

    def test_worker_replace_preserves_cao_namespace(self):
        """PATCH with worker payload does not erase the cao namespace."""
        _, TestSessionLocal = _make_engine_and_session()
        _insert_terminal(
            TestSessionLocal,
            "t1",
            "grok_cli",
            json.dumps({"cao": {"config_sha256": "abc123"}, "user_key": "old"}),
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", TestSessionLocal):
            from cli_agent_orchestrator.clients.database import (
                merge_terminal_system_metadata,
                read_terminal_system_metadata,
                update_terminal_metadata,
            )

            # Worker does a full replace (simulating PATCH /terminals/t1/metadata)
            result = update_terminal_metadata("t1", {"attacker": "payload"})
            assert result is True

            # Read back
            with TestSessionLocal() as db:
                t = db.query(TerminalModel).filter(TerminalModel.id == "t1").first()
                stored = json.loads(t.metadata_json)

            # The worker's key is there
            assert stored.get("attacker") == "payload"
            # The old user_key is gone (full replace semantics)
            assert "user_key" not in stored
            # But the system namespace survives
            assert stored.get("cao") == {"config_sha256": "abc123"}

            # System metadata reads correctly
            sys_meta = read_terminal_system_metadata("t1")
            assert sys_meta == {"config_sha256": "abc123"}

    def test_worker_cannot_inject_cao_key(self):
        """A worker including 'cao' in their payload has it stripped."""
        _, TestSessionLocal = _make_engine_and_session()
        _insert_terminal(
            TestSessionLocal,
            "t2",
            "grok_cli",
            json.dumps({"cao": {"config_sha256": "real_hash"}}),
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", TestSessionLocal):
            from cli_agent_orchestrator.clients.database import (
                read_terminal_system_metadata,
                update_terminal_metadata,
            )

            # Worker tries to overwrite cao
            update_terminal_metadata("t2", {"cao": {"evil": "inject"}, "normal": "data"})

            # System namespace unchanged — worker's cao was stripped
            sys_meta = read_terminal_system_metadata("t2")
            assert sys_meta == {"config_sha256": "real_hash"}

    def test_merge_terminal_system_metadata_upserts(self):
        """merge_terminal_system_metadata does read-modify-write correctly."""
        _, TestSessionLocal = _make_engine_and_session()
        _insert_terminal(
            TestSessionLocal,
            "t3",
            "grok_cli",
            json.dumps({"cao": {"config_sha256": "hash1"}, "other": "data"}),
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", TestSessionLocal):
            from cli_agent_orchestrator.clients.database import (
                merge_terminal_system_metadata,
                read_terminal_system_metadata,
            )

            # Merge additional key without disturbing existing
            merge_terminal_system_metadata("t3", {"wedge_suspect": True})

            sys_meta = read_terminal_system_metadata("t3")
            assert sys_meta == {"config_sha256": "hash1", "wedge_suspect": True}

            # Verify non-cao metadata undisturbed
            with TestSessionLocal() as db:
                t = db.query(TerminalModel).filter(TerminalModel.id == "t3").first()
                stored = json.loads(t.metadata_json)
            assert stored.get("other") == "data"

    def test_clearing_metadata_preserves_cao(self):
        """update_terminal_metadata(id, None) preserves the cao namespace."""
        _, TestSessionLocal = _make_engine_and_session()
        _insert_terminal(
            TestSessionLocal,
            "t4",
            "grok_cli",
            json.dumps({"cao": {"config_sha256": "keep"}, "worker_data": "x"}),
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", TestSessionLocal):
            from cli_agent_orchestrator.clients.database import (
                read_terminal_system_metadata,
                update_terminal_metadata,
            )

            # Worker clears metadata
            update_terminal_metadata("t4", None)

            # System namespace preserved
            sys_meta = read_terminal_system_metadata("t4")
            assert sys_meta == {"config_sha256": "keep"}


# ---------------------------------------------------------------------------
# AC13: Half 1 stamps survive the redeploy (legacy fallback)
# ---------------------------------------------------------------------------


class TestLegacyFallback:
    """Rows with legacy top-level config_sha256 still evaluate correctly."""

    def test_is_config_stale_legacy_row(self):
        """_is_config_stale reads legacy top-level config_sha256."""
        from cli_agent_orchestrator.services.fleet_service import _is_config_stale

        # Legacy row: config_sha256 at top level, no 'cao' key
        row_matching = {
            "provider": "grok_cli",
            "metadata": {"config_sha256": "abc123"},
        }
        assert _is_config_stale(row_matching, "abc123") is False

        row_stale = {
            "provider": "grok_cli",
            "metadata": {"config_sha256": "old_hash"},
        }
        assert _is_config_stale(row_stale, "abc123") is True

    def test_is_config_stale_new_namespace(self):
        """_is_config_stale prefers 'cao' namespace when available."""
        from cli_agent_orchestrator.services.fleet_service import _is_config_stale

        row = {
            "provider": "grok_cli",
            "metadata": {
                "cao": {"config_sha256": "new_hash"},
                "config_sha256": "old_hash",  # legacy, should be ignored
            },
        }
        # Should use cao namespace value
        assert _is_config_stale(row, "new_hash") is False
        assert _is_config_stale(row, "other") is True

    def test_count_stale_legacy_and_new(self):
        """_count_stale_grok_terminals handles both legacy and new formats."""
        from cli_agent_orchestrator.services.grok_config_watcher import (
            _count_stale_grok_terminals,
        )

        _, TestSessionLocal = _make_engine_and_session()
        # Legacy row — stale
        _insert_terminal(TestSessionLocal, "t1", "grok_cli", json.dumps({"config_sha256": "old"}))
        # New namespace row — matching (not stale)
        _insert_terminal(
            TestSessionLocal, "t2", "grok_cli", json.dumps({"cao": {"config_sha256": "current"}})
        )
        # New namespace row — stale
        _insert_terminal(
            TestSessionLocal, "t3", "grok_cli", json.dumps({"cao": {"config_sha256": "old"}})
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", TestSessionLocal):
            count = _count_stale_grok_terminals("current")
            # t1 (legacy, stale) + t3 (new, stale) = 2
            assert count == 2
