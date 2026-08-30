"""F643b (#498): codex 0.151.0 SQLite confirmation path + scoped pane safety net.

Second-order follow-up to F643. The post-merge live probe (terminal c4c837b8,
codex-cli 0.151.0) failed the SAME "structurally unconfirmed" way, but forensics
showed a DIFFERENT substrate: codex 0.151.0 no longer writes interactive turns
to ``~/.codex/sessions/**/rollout-*.jsonl`` — it writes them to a SQLite store
at the CODEX_HOME root (``thread_history_<shard>.sqlite``). The JSONL rollout is
now only the legacy ``codex exec`` SEED artifact. So F435's rollout-JSONL signal
is BLIND on 0.151.0 and every fresh codex worker was torn down at init.

Empirically confirmed schema (from the real 45MB ~/.codex/thread_history_1.sqlite):
  thread_items(thread_id, turn_id, item_id, rollout_ordinal, created_at_ms,
               item_json, item_type, ...)
  - thread_id == the codex session uuid CAO pins as provider_session_id
    (a resume writes to the SAME thread — no forked-id pinning gap).
  - userMessage item_json: {"type":"userMessage","content":[{"type":"text","text":"…"}]}
  - created_at_ms bounds the dispatch (excludes the seed's SEED_OK turn).

This suite models the 0.151.0 world (SQLite rows, silent JSONL) and the SCOPED
pane safety net (B, user ruling OPTION 2): confirm-by-pane fires ONLY when a
thread_history SQLite EXISTS (0.151.0) AND is silent AND the pane shows submitted.
The mutant removing the sqlite-exists gate must flip the old-world (r7) case back
to raising — proving the scoping is load-bearing.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.codex import (
    CodexProvider,
    CodexSubmitBaseline,
    CodexSubmitStuckError,
)

METADATA_BASE = {"tmux_session": "sess", "tmux_window": "win"}
FOOTER = "  ~/VScode_projects/cli-subagents · main · gpt-5.6-sol high"
SESSION_UUID = "01a05508-9adc-73e0-a0bb-5c0da078415c"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("cli_agent_orchestrator.providers.codex.time.sleep", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _fast_monotonic():
    counter = {"t": 0.0}

    def _mono():
        counter["t"] += 100.0
        return counter["t"]

    with patch("cli_agent_orchestrator.providers.codex.time.monotonic", side_effect=_mono):
        yield


def _provider() -> CodexProvider:
    return CodexProvider("term1234", "sess", "win")


def _metadata(session_uuid: str = SESSION_UUID) -> dict[str, Any]:
    return {**METADATA_BASE, "provider_session_id": session_uuid}


def _backend_no_chip() -> MagicMock:
    backend = MagicMock()
    backend.get_history.return_value = (
        "• SEED_OK\n\n› Ask Codex to do anything\n\n" + FOOTER + "\n"
    )
    return backend


def _backend_submitted_turn(msg: str) -> MagicMock:
    """Pane showing a NEW submitted turn for our dispatch + cleared composer.

    Uses the raw task text as a submitted history line above the idle composer,
    so _pane_shows_new_submitted_task (vs a baseline that lacks it) returns
    SUBMITTED.
    """
    backend = MagicMock()
    pane = (
        "• SEED_OK\n\n"
        f"› {msg}\n\n"
        "• On it.\n\n"
        "› Ask Codex to do anything\n\n"
        f"{FOOTER}\n"
    )
    backend.get_history.return_value = pane
    return backend


def _enter_calls(backend: MagicMock) -> int:
    return sum(
        1
        for call in backend.send_special_key.call_args_list
        if "Enter" in call.args or call.kwargs.get("key") == "Enter"
    )


@pytest.fixture()
def patched_codex_home(tmp_path: Path):
    with patch(
        "cli_agent_orchestrator.providers.codex._resolved_codex_home",
        return_value=tmp_path,
    ):
        yield tmp_path


def _seed_rollout(home: Path) -> Path:
    """A stale seed rollout JSONL (the legacy `codex exec` artifact)."""
    sessions = home / "sessions" / "2026" / "08" / "30"
    sessions.mkdir(parents=True, exist_ok=True)
    seed = sessions / f"rollout-2026-08-30T19-37-02-{SESSION_UUID}.jsonl"
    with seed.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {"id": SESSION_UUID}}) + "\n")
        # pad to a realistic seed offset
        f.write(
            json.dumps(
                {"type": "event_msg", "payload": {"type": "assistant_message", "message": "x" * 49000}}
            )
            + "\n"
        )
    return seed


def _make_thread_history_db(home: Path) -> Path:
    """Create a thread_history_1.sqlite with the real 0.151.0 schema."""
    db = home / "thread_history_1.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE thread_items ("
        "thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL, "
        "rollout_ordinal INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, "
        "item_json TEXT NOT NULL, item_type TEXT NOT NULL DEFAULT '', "
        "updated_at_ordinal INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (thread_id, turn_id, item_id))"
    )
    con.commit()
    con.close()
    return db


def _insert_user_message(db: Path, thread_id: str, text: str, created_at_ms: int, *, ordinal: int = 10) -> None:
    con = sqlite3.connect(db)
    item_json = json.dumps(
        {"type": "userMessage", "id": f"itm-{ordinal}", "clientId": None,
         "content": [{"type": "text", "text": text, "text_elements": []}]}
    )
    con.execute(
        "INSERT INTO thread_items(thread_id, turn_id, item_id, rollout_ordinal, "
        "created_at_ms, item_json, item_type) VALUES (?,?,?,?,?,?,?)",
        (thread_id, f"turn-{ordinal}", f"itm-{ordinal}", ordinal, created_at_ms, item_json, "userMessage"),
    )
    con.commit()
    con.close()


def _baseline(seed: Path, *, baseline_wall: float, captured_ok: bool = True) -> CodexSubmitBaseline:
    return CodexSubmitBaseline(
        rollout_path=seed,
        rollout_offset=seed.stat().st_size if seed.exists() else 0,
        baseline_wall=baseline_wall,
        captured_ok=captured_ok,
    )


MSG = "Root-cause and fix F643b per the assignment [callback: terminal 9064394e]"


# ===========================================================================
# (A) SQLite confirmation path
# ===========================================================================


class TestSqliteConfirmation:
    def test_task_in_sqlite_confirms(self, patched_codex_home: Path):
        """0.151.0 world: JSONL silent, task turn in SQLite after baseline → confirm."""
        home = patched_codex_home
        seed = _seed_rollout(home)
        baseline_wall = time.time()
        db = _make_thread_history_db(home)
        _insert_user_message(db, SESSION_UUID, MSG, int((baseline_wall + 2) * 1000))

        provider = _provider()
        backend = _backend_no_chip()
        provider.verify_submission_after_send(_metadata(), backend, message=MSG, baseline=_baseline(seed, baseline_wall=baseline_wall))
        assert _enter_calls(backend) == 0

    def test_sqlite_row_before_baseline_does_not_confirm(self, patched_codex_home: Path):
        """A SQLite userMessage stamped BEFORE the dispatch (the seed turn) must not confirm."""
        home = patched_codex_home
        seed = _seed_rollout(home)
        baseline_wall = time.time()
        db = _make_thread_history_db(home)
        # Seed turn, before baseline.
        _insert_user_message(db, SESSION_UUID, MSG, int((baseline_wall - 5) * 1000))

        provider = _provider()
        backend = _backend_no_chip()  # pane idle, no new submitted turn
        with pytest.raises(CodexSubmitStuckError, match="structurally unconfirmed"):
            provider.verify_submission_after_send(_metadata(), backend, message=MSG, baseline=_baseline(seed, baseline_wall=baseline_wall))

    def test_sqlite_wrong_thread_does_not_confirm(self, patched_codex_home: Path):
        """A matching turn on a DIFFERENT thread_id must not confirm ours."""
        home = patched_codex_home
        seed = _seed_rollout(home)
        baseline_wall = time.time()
        db = _make_thread_history_db(home)
        _insert_user_message(db, "some-other-thread-uuid", MSG, int((baseline_wall + 2) * 1000))

        provider = _provider()
        backend = _backend_no_chip()
        with pytest.raises(CodexSubmitStuckError, match="structurally unconfirmed"):
            provider.verify_submission_after_send(_metadata(), backend, message=MSG, baseline=_baseline(seed, baseline_wall=baseline_wall))

    def test_sqlite_content_mismatch_does_not_confirm(self, patched_codex_home: Path):
        """A userMessage with different content must not confirm."""
        home = patched_codex_home
        seed = _seed_rollout(home)
        baseline_wall = time.time()
        db = _make_thread_history_db(home)
        _insert_user_message(db, SESSION_UUID, "an entirely different task", int((baseline_wall + 2) * 1000))

        provider = _provider()
        backend = _backend_no_chip()
        with pytest.raises(CodexSubmitStuckError, match="structurally unconfirmed"):
            provider.verify_submission_after_send(_metadata(), backend, message=MSG, baseline=_baseline(seed, baseline_wall=baseline_wall))

    def test_sqlite_helper_direct(self, patched_codex_home: Path):
        """Unit-level: _sqlite_has_user_event positive + guards."""
        home = patched_codex_home
        _seed_rollout(home)
        baseline_wall = time.time()
        db = _make_thread_history_db(home)
        _insert_user_message(db, SESSION_UUID, MSG, int((baseline_wall + 2) * 1000))
        provider = _provider()
        assert provider._sqlite_has_user_event(SESSION_UUID, MSG, baseline_wall) is True
        assert provider._sqlite_has_user_event(SESSION_UUID, "", baseline_wall) is False
        assert provider._sqlite_has_user_event(SESSION_UUID, MSG, 0.0) is False
        assert provider._sqlite_has_user_event(None, MSG, baseline_wall) is False
        assert provider._sqlite_has_user_event(SESSION_UUID, "different", baseline_wall) is False


# ===========================================================================
# (B) SCOPED pane safety net + the load-bearing sqlite-exists gate
# ===========================================================================


class TestScopedPaneSafetyNet:
    def test_sqlite_present_silent_pane_submitted_confirms_by_pane(self, patched_codex_home: Path, caplog):
        """The residual-risk case (user directive): SQLite EXISTS, stays silent
        through exhaustion, but the pane shows submitted → confirm-by-pane WARNING."""
        import logging

        home = patched_codex_home
        seed = _seed_rollout(home)
        baseline_wall = time.time()
        # SQLite DB exists but has NO matching row (silent).
        _make_thread_history_db(home)

        provider = _provider()
        backend = _backend_submitted_turn(MSG)  # pane shows our submitted turn
        with caplog.at_level(logging.WARNING):
            provider.verify_submission_after_send(_metadata(), backend, message=MSG, baseline=_baseline(seed, baseline_wall=baseline_wall))
        assert _enter_calls(backend) == 0
        # Three-fact log line present.
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "confirmed-by-pane" in joined
        assert "sqlite-present=True" in joined
        assert "sqlite-silent=True" in joined
        assert "pane-submitted=True" in joined

    def test_no_sqlite_pane_submitted_still_raises_oldworld(self, patched_codex_home: Path):
        """Old-world (no SQLite DB): pane-submitted + JSONL silent → STILL RAISES.

        This is the r7 B1 invariant preserved. It is ALSO the mutant target: if
        the sqlite-exists gate were removed, this case would flip to confirm.
        """
        home = patched_codex_home
        seed = _seed_rollout(home)
        baseline_wall = time.time()
        # NO thread_history sqlite created → old JSONL world.

        provider = _provider()
        backend = _backend_submitted_turn(MSG)  # pane shows submitted
        with pytest.raises(CodexSubmitStuckError, match="structurally unconfirmed"):
            provider.verify_submission_after_send(_metadata(), backend, message=MSG, baseline=_baseline(seed, baseline_wall=baseline_wall))

    def test_sqlite_present_pane_unsubmitted_still_raises(self, patched_codex_home: Path):
        """SQLite exists + silent, but pane shows NO new submitted turn (task still
        drafted / idle) → must RAISE so the deferral path re-attempts the SUBMIT."""
        home = patched_codex_home
        seed = _seed_rollout(home)
        baseline_wall = time.time()
        _make_thread_history_db(home)  # exists, silent

        provider = _provider()
        backend = _backend_no_chip()  # no new submitted turn in pane
        with pytest.raises(CodexSubmitStuckError, match="structurally unconfirmed"):
            provider.verify_submission_after_send(_metadata(), backend, message=MSG, baseline=_baseline(seed, baseline_wall=baseline_wall))
