"""Test isolation for the mcp_server unit suite.

F689 (#544): ``_assign_impl`` consults ``GET /terminals/<caller>`` through
``_configured_default_fork_base`` whenever ``fork_from`` is omitted. Most tests
in this package set ``CAO_TERMINAL_ID`` to the fixture id ``abcd1234`` and stub
only ``_create_terminal`` / ``strict_supervisor_cwd``, so that lookup went out on
the wire to the LIVE production server on :9889 — the source of the 48-hit
``abcd1234`` burst in ``cao_2026-09-01_04-28-22.log``, interleaved with real
status_monitor traffic for the supervisor seat.

The lookup only resolves a *default* fork base, which no unit test here is
asserting on (the tests that exercise fork bases pass ``fork_from`` explicitly,
which skips this call entirely). Stubbing it to "no configured default" is
therefore behaviour-preserving for this package and removes the only unstubbed
live call in it.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.mcp_server import server


@pytest.fixture(autouse=True)
def _no_live_default_fork_base_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_configured_default_fork_base", lambda agent_profile: None)
