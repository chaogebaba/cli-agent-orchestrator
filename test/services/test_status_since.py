"""D11 / I5 — ``status_since`` on the fleet row, and the null that goes with it.

I5 states the property as an equality AND a null: the fleet row's
``status_since`` equals ``worker_state_shadow.since`` for a projected terminal,
and is null for an unsourced one. The null is the load-bearing half — a run
where it is non-null for an unsourced terminal fails the criterion, because that
would be a reconstruction from ``last_active`` or from the fleet's own polling
wearing the projection's name.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.services.fleet_service import _status_since

TERMINAL = "t-since"
SINCE = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _runtime(since: datetime | None) -> MagicMock:
    runtime = MagicMock()
    if since is None:
        runtime.state_store.get.return_value = None
    else:
        row = MagicMock()
        row.since = since
        runtime.state_store.get.return_value = row
    return runtime


def _read(*, projected: bool, runtime: object) -> str | None:
    monitor = MagicMock()
    monitor.is_projected.return_value = projected
    with (
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor", monitor),
        patch("cli_agent_orchestrator.bootstrap.current_runtime", return_value=runtime),
    ):
        return _status_since(TERMINAL)


def test_a_projected_terminal_reports_the_projection_s_own_moment() -> None:
    assert _read(projected=True, runtime=_runtime(SINCE)) == SINCE.isoformat()


def test_an_unsourced_terminal_reports_nothing() -> None:
    """The half that fails a run rather than merely looking empty.

    The pane path has no such moment to report: it publishes a status without
    ever recording when the terminal entered it.  Anything non-null here would
    be reconstructed, and a reconstruction under this key would be read as the
    projection's answer.
    """
    assert _read(projected=False, runtime=_runtime(SINCE)) is None


def test_the_projection_is_not_even_read_for_an_unsourced_terminal() -> None:
    """The gate comes first, so the fleet path does not pay a database read for
    an answer it is going to discard."""
    runtime = _runtime(SINCE)

    _read(projected=False, runtime=runtime)

    runtime.state_store.get.assert_not_called()


def test_a_projected_terminal_with_no_row_yet_reports_nothing() -> None:
    assert _read(projected=True, runtime=_runtime(None)) is None


def test_with_worker_truth_off_it_reports_nothing() -> None:
    assert _read(projected=True, runtime=None) is None


def test_a_broken_read_never_breaks_the_fleet() -> None:
    """The fleet renders without the key rather than not at all."""
    runtime = MagicMock()
    runtime.state_store.get.side_effect = RuntimeError("database unavailable")

    assert _read(projected=True, runtime=runtime) is None
