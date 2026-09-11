"""Terminal teardown drops WP-ARCH's per-terminal state (phase 2, S7).

Terminal ids are recycled.  The projected mark gates phase 2's status cutover, so
a recycled id inheriting a dead terminal's ``True`` would have its pane path
suppressed on the strength of a source that belonged to something else — a worker
publishing nothing at all.  The producers' edge maps are the same leak in a
milder form: they grow one entry per terminal for the life of the process, and a
recycled id would inherit an edge it never crossed and so miss the first real one.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.adapters.truth import legacy_egress, pane_classification
from cli_agent_orchestrator.app.worker_truth.checks import ProducerDisagreementCheck
from cli_agent_orchestrator.app.worker_truth.health import SourceHealth
from cli_agent_orchestrator.services.terminal_service import forget_worker_truth_state

TERMINAL = "term-gone"


@dataclass
class _Runtime:
    health: SourceHealth | None
    producer_check: ProducerDisagreementCheck | None = None


@pytest.fixture(autouse=True)
def _clean_edges():
    pane_classification.reset_edges()
    legacy_egress.reset_edges()
    yield
    pane_classification.reset_edges()
    legacy_egress.reset_edges()


def test_the_projected_mark_does_not_survive_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health = SourceHealth()
    health.mark(TERMINAL, projected=True)
    monkeypatch.setattr(bootstrap, "current_runtime", lambda: _Runtime(health=health))

    forget_worker_truth_state(TERMINAL)

    assert health.is_projected(TERMINAL) is False


def test_the_producers_edge_state_goes_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recycled id must cross its first edge, not inherit the last one."""
    monkeypatch.setattr(bootstrap, "current_runtime", lambda: _Runtime(health=SourceHealth()))
    pane_classification._last_pair[TERMINAL] = ("idle", "incremental")
    pane_classification._last_condition[TERMINAL] = "CAPPED"
    pane_classification._edge_seq[TERMINAL] = 7
    legacy_egress._last_pair[TERMINAL] = ("idle", "incremental")
    legacy_egress._last_event_id[TERMINAL] = "01ABC"

    forget_worker_truth_state(TERMINAL)

    assert TERMINAL not in pane_classification._last_pair
    assert TERMINAL not in pane_classification._last_condition
    assert TERMINAL not in pane_classification._edge_seq
    assert TERMINAL not in legacy_egress._last_pair
    assert legacy_egress.last_published_event_id(TERMINAL) is None


def test_the_disagreement_episode_goes_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """R3's episode map is per-terminal memory too.

    A recycled id inheriting an open episode would have its FIRST real
    disagreement swallowed as a repeat of a dead terminal's.
    """

    class _Findings:
        def record(self, *args: object, **kwargs: object) -> None:
            return None

    check = ProducerDisagreementCheck(_Findings())
    check._open[TERMINAL] = ("idle", "busy")
    monkeypatch.setattr(
        bootstrap,
        "current_runtime",
        lambda: _Runtime(health=SourceHealth(), producer_check=check),
    )

    forget_worker_truth_state(TERMINAL)

    assert TERMINAL not in check._open


def test_no_runtime_at_all_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deletes happen with worker truth switched off, and far more often."""
    monkeypatch.setattr(bootstrap, "current_runtime", lambda: None)

    forget_worker_truth_state(TERMINAL)  # must not raise


def test_a_broken_runtime_never_breaks_a_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Teardown is not the place to discover a diagnostics bug."""

    def boom() -> object:
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(bootstrap, "current_runtime", boom)

    forget_worker_truth_state(TERMINAL)  # must not raise


def test_the_delete_path_calls_it() -> None:
    """The wiring, asserted where it actually lives.

    ``delete_terminal`` is a long function behind a delivery lock and a database;
    what this pins is that the universal delete path still names this cleanup,
    beside the ``pane_liveness`` and ``question_state`` ones it sits with.
    """
    from pathlib import Path

    import cli_agent_orchestrator.services.terminal_service as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    body = source[source.index("def forget_worker_truth_state") :]
    assert "forget_worker_truth_state(terminal_id)" in body.split("\n\n\n", 1)[1]
