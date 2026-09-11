"""What a live acceptance round needs from the product to be measurable at all.

Three defects the first box round with the delivery queue armed hit, none of
which is a delivery bug and all of which stopped an acceptance criterion from
being measured. They are collected here because that is what they have in
common: the box live round is now the ONLY acceptance surface for a flag flip,
so a product limit that makes an arm unmeasurable is a product defect and not a
round-procedure footnote.

* #742 -- the pane read the copy-count criterion needs was capped at 200 lines
  and the HTTP route rejected more with 422.
* #743 -- a persona-bearing profile could not START on a host with no
  ``XDG_RUNTIME_DIR``, which is every box, so the AC10 agreement report was
  INVALID for want of a codex terminal.
* #744 -- a read-only recount of a database whose writer died uncleanly reports
  the last checkpoint and says nothing about the WAL it could not read.
"""

from __future__ import annotations

import inspect
import logging
import os
import sqlite3
import stat
from pathlib import Path

import pytest

# --------------------------------------------------------------------- #742


def test_the_pane_read_reaches_past_one_screen_of_scrollback() -> None:
    """The transport ceiling and the MCP context budget are different numbers.

    200 was one quantity doing duty as both. A seat pays for every line it peeks
    and keeps its cap; an out-of-band probe pays nothing and needs enough
    scrollback to count copies across a 30-message round.
    """
    from cli_agent_orchestrator.api import routes_fork
    from cli_agent_orchestrator.services.terminal_service import (
        MAX_PEEK_TERMINAL_LINES,
        MCP_PEEK_TERMINAL_LINES,
    )

    assert MAX_PEEK_TERMINAL_LINES >= 400, "a 30-message round does not fit in 200 lines"
    assert MCP_PEEK_TERMINAL_LINES == 200, "the seat's context budget is not the thing being raised"

    query = inspect.signature(routes_fork.peek_terminal).parameters["lines"].default
    ceilings = [getattr(c, "le", None) for c in query.metadata]
    assert MAX_PEEK_TERMINAL_LINES in ceilings, (
        "the route rejected what the service would have clamped: two layers "
        "disagreeing about the limit is what produced the 422 (#742)"
    )


def test_the_service_still_clamps_rather_than_raising() -> None:
    """Raising the ceiling must not turn an over-large ask into an exception."""
    from cli_agent_orchestrator.services import terminal_service

    captured: dict[str, object] = {}

    class _Backend:
        def get_history(self, session, window, *, tail_lines, strip_escapes):
            captured["tail_lines"] = tail_lines
            return ""

    original_meta = terminal_service.get_terminal_metadata
    original_backend = terminal_service.get_backend
    terminal_service.get_terminal_metadata = lambda tid: {  # type: ignore[assignment]
        "tmux_session": "s",
        "tmux_window": "w",
    }
    terminal_service.get_backend = lambda: _Backend()  # type: ignore[assignment]
    try:
        terminal_service.peek_terminal("t", 10_000)
    finally:
        terminal_service.get_terminal_metadata = original_meta  # type: ignore[assignment]
        terminal_service.get_backend = original_backend  # type: ignore[assignment]

    assert captured["tail_lines"] == terminal_service.MAX_PEEK_TERMINAL_LINES


# --------------------------------------------------------------------- #743


def test_a_persona_starts_on_a_host_with_no_runtime_dir(tmp_path, monkeypatch) -> None:
    """No ``XDG_RUNTIME_DIR`` is a host without a systemd user session, not a
    misconfiguration -- and it is what every grok box is.

    The fallback keeps the part that matters: the tree is still owner-only.
    """
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.utils import persona_context

    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    home = tmp_path / "cao-home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(constants, "CAO_HOME_DIR", home)

    root = persona_context._persona_root()

    assert root.is_dir()
    assert home in root.parents
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert root.stat().st_uid == os.getuid()


def test_an_explicitly_bad_runtime_dir_still_fails_loud(monkeypatch) -> None:
    """The two cases are not symmetric, deliberately.

    A variable someone SET to a relative path is a mistake to report; silently
    relocating a persona to somewhere they did not name is worse than refusing.
    """
    from cli_agent_orchestrator.utils import persona_context

    monkeypatch.setenv("XDG_RUNTIME_DIR", "relative/not/absolute")
    with pytest.raises(persona_context.PersonaContextError, match="persona_runtime_dir_invalid"):
        persona_context._persona_root()


# --------------------------------------------------------------------- #744


def test_a_read_only_open_says_when_the_wal_may_be_unreadable(tmp_path, caplog) -> None:
    """A count that is too LOW with no error is worse than an error.

    Reproduces the shape directly: commit into the WAL, drop the ``-shm`` the way
    an unclean death leaves it, then open read-only. The connection still opens
    -- the point is that it now says what it may be missing.
    """
    from cli_agent_orchestrator.adapters.store.connection import read_only_connect

    db_path = tmp_path / "live.sqlite"
    writer = sqlite3.connect(db_path, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    writer.execute("INSERT INTO t (id) VALUES (1)")
    writer.close()

    wal = Path(str(db_path) + "-wal")
    shm = Path(str(db_path) + "-shm")
    wal.write_bytes(b"\x00" * 4096)
    if shm.exists():
        shm.unlink()

    caplog.set_level(logging.WARNING, logger="cli_agent_orchestrator.adapters.store.connection")
    conn = read_only_connect(db_path, busy_timeout_ms=1000)
    try:
        assert any("un-checkpointed WAL" in r.message for r in caplog.records), (
            "a stale read-only view reported nothing, so a recount could not tell "
            "a surviving queue from a lost one (#744)"
        )
    finally:
        conn.close()


def test_a_healthy_read_only_open_stays_quiet(tmp_path, caplog) -> None:
    """The warning has to be rare enough to mean something when it fires."""
    from cli_agent_orchestrator.adapters.store.connection import read_only_connect

    db_path = tmp_path / "quiet.sqlite"
    writer = sqlite3.connect(db_path, isolation_level=None)
    writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    writer.close()

    caplog.set_level(logging.WARNING, logger="cli_agent_orchestrator.adapters.store.connection")
    conn = read_only_connect(db_path, busy_timeout_ms=1000)
    try:
        assert not [r for r in caplog.records if "un-checkpointed WAL" in r.message]
    finally:
        conn.close()
