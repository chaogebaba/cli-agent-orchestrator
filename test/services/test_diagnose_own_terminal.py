"""F99 acceptance tests for diagnose_own_terminal (AC1, AC2, AC5, AC6, AC7)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import identity_verify_service as idservice


def _database(tmp_path: Path, *rows: tuple[str, str, str, str | None]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "cao.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE terminals ("
        "id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, tmux_window TEXT NOT NULL, "
        "agent_profile TEXT)"
    )
    connection.executemany(
        "INSERT INTO terminals (id, tmux_session, tmux_window, agent_profile) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()
    return path


def _identity_self(pid: int, *, ancestor_of: int | None = None) -> bool:
    target = ancestor_of if ancestor_of is not None else 4242
    return pid == target


@pytest.fixture(autouse=True)
def _clear_cache():
    idservice._diag_cache.clear()
    yield
    idservice._diag_cache.clear()


class TestNoPane:
    def test_tmux_pane_absent_fails_closed(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TMUX_PANE", raising=False)
        db = _database(tmp_path, ("abcd1234", "s", "w", "worker"))
        result = idservice.diagnose_own_terminal("abcd1234", db_path=db)
        assert result["branch"] == "no_pane"


class TestSelfProofFailClosed:
    def test_pane_pid_not_self_or_ancestor_is_not_trusted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMUX_PANE", "%0")
        monkeypatch.setattr(idservice, "_resolve_pane_window", lambda pane: ("s", "w"))
        monkeypatch.setattr(idservice.TmuxBackend, "_pane_pids", lambda self, s, w: [99999])
        db = _database(tmp_path, ("abcd1234", "s", "w", "worker"))
        result = idservice.diagnose_own_terminal(
            "abcd1234", db_path=db, pane_pid_self=lambda pid: False
        )
        assert result["branch"] == "self_proof_fail"
        assert result["pane_pid"] == 99999
        # No row is ever adopted on a distrusted pane.
        assert result.get("db_matches") is None


class TestPassBranch:
    def test_row_gone_when_self_proof_passes_and_no_db_row_claims_window(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("TMUX_PANE", "%0")
        monkeypatch.setattr(idservice, "_resolve_pane_window", lambda pane: ("s", "w"))
        monkeypatch.setattr(idservice.TmuxBackend, "_pane_pids", lambda self, s, w: [4242])
        db = _database(tmp_path)  # no rows -> row gone
        result = idservice.diagnose_own_terminal(
            "abcd1234", db_path=db, pane_pid_self=lambda pid: pid == 4242
        )
        assert result["branch"] == "row_gone"
        assert result["session"] == "s"
        assert result["window"] == "w"
        assert result["pane_pid"] == 4242
        assert result["db_matches"] == []


class TestAmbiguous:
    def test_multiple_rows_claiming_window_are_reported_not_picked(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMUX_PANE", "%0")
        monkeypatch.setattr(idservice, "_resolve_pane_window", lambda pane: ("s", "w"))
        monkeypatch.setattr(idservice.TmuxBackend, "_pane_pids", lambda self, s, w: [4242])
        db = _database(
            tmp_path,
            ("aaaa1111", "s", "w", "worker"),
            ("bbbb2222", "s", "w", "worker"),
        )
        result = idservice.diagnose_own_terminal(
            "abcd1234", db_path=db, pane_pid_self=lambda pid: pid == 4242
        )
        assert result["branch"] == "ambiguous"
        assert result["db_matches"] == ["aaaa1111", "bbbb2222"]


class TestCache:
    def test_positive_result_cached_within_ttl_single_probe(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMUX_PANE", "%0")
        monkeypatch.setattr(idservice, "_resolve_pane_window", lambda pane: ("s", "w"))
        monkeypatch.setattr(idservice.TmuxBackend, "_pane_pids", lambda self, s, w: [4242])
        db = _database(tmp_path)
        calls = {"n": 0}

        def pane_pids(self, s, w):
            calls["n"] += 1
            return [4242]

        monkeypatch.setattr(idservice.TmuxBackend, "_pane_pids", pane_pids)
        idservice.diagnose_own_terminal(
            "abcd1234", db_path=db, pane_pid_self=lambda pid: pid == 4242
        )
        idservice.diagnose_own_terminal(
            "abcd1234", db_path=db, pane_pid_self=lambda pid: pid == 4242
        )
        assert calls["n"] == 1

    def test_negative_self_proof_fail_not_cached_reprobes(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMUX_PANE", "%0")
        monkeypatch.setattr(idservice, "_resolve_pane_window", lambda pane: ("s", "w"))
        db = _database(tmp_path)
        calls = {"n": 0}

        def pane_pids(self, s, w):
            calls["n"] += 1
            return [99999]

        monkeypatch.setattr(idservice.TmuxBackend, "_pane_pids", pane_pids)
        idservice.diagnose_own_terminal("abcd1234", db_path=db, pane_pid_self=lambda pid: False)
        idservice.diagnose_own_terminal("abcd1234", db_path=db, pane_pid_self=lambda pid: False)
        assert calls["n"] == 2


class TestF94RPrimitivesReused:
    def test_no_pane_or_db_read_logic_in_server_side(self):
        """AC7: the seam calls diagnose_own_terminal; server.py must not
        re-implement pane/DB reads. Structural guard: the service holds the
        pane/DB primitives and diagnose_own_terminal is the single entry point."""
        import re
        from pathlib import Path

        server_src = Path(
            Path(__file__).resolve().parents[2] / "src/cli_agent_orchestrator/mcp_server/server.py"
        ).read_text(encoding="utf-8")
        # server.py must not shell out to tmux for pane identity or read the DB.
        assert "diagnose_own_terminal" in server_src
        assert "list-panes" not in server_src
        assert "tmux display-message" not in server_src
        assert re.search(r"SELECT .* FROM terminals", server_src) is None
