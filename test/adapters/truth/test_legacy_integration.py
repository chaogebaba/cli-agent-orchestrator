"""AC11 — the hooks fire through the REAL legacy functions (F725 #581, lane B).

``test_hook_points.py`` proves the hooks are in the right PLACE.  These tests
prove they are actually reached, and — the half that matters more — that with the
switch off the legacy functions behave exactly as they did before: same return,
same exception type, same message, and not one row written.

Each test is written as an ON/OFF pair against the same call, so the OFF half is
never merely the absence of an assertion.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.core.events import DecisionKind, EventKind
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.status_monitor import status_monitor

from .conftest import FakeEventStore

MISSING = "no-such-terminal-f725"


# -- hook 3, through the real dispatch functions ----------------------------


def test_send_input_off_behaves_exactly_as_before(store: FakeEventStore) -> None:
    with pytest.raises(ValueError) as caught:
        terminal_service.send_input(MISSING, "hello")
    assert f"Terminal '{MISSING}' not found" in str(caught.value)
    assert store.rows == []


def test_send_input_on_records_one_attempt_and_re_raises(
    ingest_on: FakeEventStore,
) -> None:
    with pytest.raises(ValueError) as caught:
        terminal_service.send_input(MISSING, "hello")
    assert f"Terminal '{MISSING}' not found" in str(caught.value)

    rows = ingest_on.of_kind(DecisionKind.DELIVERY_ATTEMPT, MISSING)
    assert len(rows) == 1
    assert rows[0].payload["carrier"] == "send_input"
    assert rows[0].payload["outcome"] == "error"
    assert "Terminal" in rows[0].payload["detail"]


def test_send_prepared_input_off_behaves_exactly_as_before(
    store: FakeEventStore,
) -> None:
    with pytest.raises(ValueError):
        terminal_service.send_prepared_input(MISSING, "hello")
    assert store.rows == []


def test_send_prepared_input_on_records_one_attempt(ingest_on: FakeEventStore) -> None:
    """This function has no outer ``try``; the decorator is why its failures are seen."""
    with pytest.raises(ValueError):
        terminal_service.send_prepared_input(MISSING, "hello")
    rows = ingest_on.of_kind(DecisionKind.DELIVERY_ATTEMPT, MISSING)
    assert len(rows) == 1
    assert rows[0].payload["carrier"] == "send_prepared_input"


def test_the_decorated_dispatch_functions_keep_their_public_identity() -> None:
    """Callers, plugins and the fork's own tests reach these by name."""
    assert terminal_service.send_input.__name__ == "send_input"
    assert terminal_service.send_prepared_input.__name__ == "send_prepared_input"
    assert terminal_service.send_input.__doc__ is not None
    assert "bracketed paste" in terminal_service.send_input.__doc__


# -- hook 2, through the real status monitor --------------------------------


def _publish(**kwargs: object) -> None:
    """Call the real ``_publish_observation`` with no metadata.

    It raises ``LookupError`` immediately afterwards, which is precisely the
    point: the hook is ABOVE that check, so an egress the monitor could not
    complete is still recorded.  Nothing is published to the receiver store, so
    the singleton is left exactly as it was found.
    """
    with pytest.raises(LookupError):
        status_monitor._publish_observation(
            "t-integration",
            latched_status="idle",
            pass_outcome="ok",
            frame_source="incremental",
            metadata=None,
            **kwargs,  # type: ignore[arg-type]
        )


def test_publish_observation_off_writes_nothing_and_still_raises(
    store: FakeEventStore,
) -> None:
    _publish(origin="incremental")
    assert store.rows == []


def test_publish_observation_on_records_the_egress(ingest_on: FakeEventStore) -> None:
    _publish(origin="incremental")
    rows = ingest_on.of_kind(EventKind.STATUS_LEGACY_PUBLISHED, "t-integration")
    assert len(rows) == 1
    assert rows[0].payload["latched_status"] == "idle"
    assert rows[0].payload["origin"] == "incremental"


def test_repeated_identical_egresses_are_still_one_row(ingest_on: FakeEventStore) -> None:
    """B9 holds through the real call path, not only through the adapter."""
    for _ in range(20):
        _publish(origin="incremental")
    assert len(ingest_on.of_kind(EventKind.STATUS_LEGACY_PUBLISHED, "t-integration")) == 1
