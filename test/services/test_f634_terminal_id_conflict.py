"""F634 (#489) D15 / AC20 — terminal_id adopt/conflict at the REAL create seam.

D15's create-route amendment adopts a caller-supplied ``terminal_id``: absent →
server-side allocation (today's behaviour); present and UNKNOWN → adopted;
present and ALREADY KNOWN on this server → refused as a conflict
(``TerminalIdConflict``), never silently re-used.

The api-level suite (``test/api/test_f634_create_route_amendment.py``) proves
the ROUTE maps ``TerminalIdConflict`` to a 409 — but it mocks the service, so
it cannot see the conflict LOGIC. This file drives the REAL
``terminal_service.create_terminal`` at its id-allocation seam with only its
resource deps stubbed, so the raise (and its absence for an unknown id) is
under test.

MUTANT (AC20 conflict clause): drop the ``raise TerminalIdConflict`` (silently
re-use a known id) → ``test_known_terminal_id_is_refused`` goes RED. The
in-tree epoch-recovery adopt precedent always supplies a FRESH id, so the
``get_terminal_metadata`` stub returning None models it — the unknown-id arm
proves the adopt path is NOT broken by the guard.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.session_lifecycle_lease import SessionLifecycleLeaseToken
from cli_agent_orchestrator.services.terminal_service import TerminalIdConflict

SUPERVISOR = "aaaaaaaa"


def _spawn_seam():
    """Patch create_terminal's resource deps while keeping the REAL body up to
    and past the D15 conflict check. ``create_window`` is a no-op that records
    nothing here — the conflict check fires long before it."""
    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.create_window.side_effect = lambda session, window, *a, **kw: window
    backend.supports_event_inbox.return_value = False
    backend.set_window_parent = None

    provider = AsyncMock()
    provider.initialize.return_value = True
    provider.shell_baseline = None

    published: list = []

    def _db_create(terminal_id, tmux_session, tmux_window, provider_name, *args, **kw):
        published.append({"id": terminal_id})
        return {"id": terminal_id}

    ids = iter(f"wrk0{i:04d}" for i in range(1, 1000))

    return (
        patch.multiple(
            ts,
            _resolve_worker_terminal_cap=lambda *a, **k: 0,
            list_terminals_by_session=lambda s: list(published),
            db_create_terminal=_db_create,
            delete_terminals_by_session=MagicMock(),
            generate_terminal_id=lambda: next(ids),
            generate_window_name=lambda profile, tid: f"{profile}-{tid}",
            provider_manager=MagicMock(create_provider=MagicMock(return_value=provider)),
            fifo_manager=MagicMock(),
            _schedule_deferred_init=MagicMock(),
            require_provider_admitted=lambda provider: None,
            load_agent_profile=lambda name: AgentProfile(name="developer", description="dev"),
            get_provider_class=lambda name: type(
                "Cap",
                (),
                {"supports_seed_resume_identity": False, "has_process_child": False},
            ),
        ),
        backend,
    )


def _lease_patches():
    return (
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease."
            "acquire_session_lifecycle_shared",
            lambda session_name: SessionLifecycleLeaseToken(
                session_name=session_name, mode="shared", nonce="t"
            ),
        ),
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease."
            "release_session_lifecycle_lease",
            lambda token: None,
        ),
    )


async def _create(*, terminal_id):
    return await ts.create_terminal(
        provider="mock_cli",
        agent_profile="developer",
        session_name="cao-f634",
        new_session=False,
        caller_id=SUPERVISOR,
        working_directory="/tmp",
        terminal_id=terminal_id,
    )


@pytest.mark.asyncio
async def test_known_terminal_id_is_refused():
    """AC20 conflict clause / MUTANT SENTINEL. A supplied id this server already
    holds (get_terminal_metadata non-None) → TerminalIdConflict, before any
    window/db create. Dropping the raise makes this go RED."""
    seam, backend = _spawn_seam()
    l1, l2 = _lease_patches()
    with (
        seam,
        l1,
        l2,
        patch("cli_agent_orchestrator.backends.registry._backend", backend),
        patch.object(ts, "get_terminal_metadata", lambda tid: {"id": tid}),
    ):
        with pytest.raises(TerminalIdConflict) as ei:
            await _create(terminal_id="deadbeef")
    assert ei.value.terminal_id == "deadbeef"
    assert ei.value.code == "E-TERMINAL-ID-CONFLICT"
    # No terminal was created — the refusal precedes the window/db seam.
    assert backend.create_window.call_count == 0


@pytest.mark.asyncio
async def test_unknown_terminal_id_is_adopted():
    """A supplied id this server does NOT hold (get_terminal_metadata None) is
    adopted, not refused — the create proceeds and the adopted id reaches the
    window/db seam. Models the epoch-recovery fresh-id adopt precedent."""
    seam, backend = _spawn_seam()
    l1, l2 = _lease_patches()
    created_ids: list = []
    orig_window = backend.create_window.side_effect

    def _record_window(session, window, terminal_id, *a, **kw):
        created_ids.append(terminal_id)
        return window

    backend.create_window.side_effect = _record_window
    with (
        seam,
        l1,
        l2,
        patch("cli_agent_orchestrator.backends.registry._backend", backend),
        patch.object(ts, "get_terminal_metadata", lambda tid: None),
    ):
        await _create(terminal_id="deadbeef")
    # The adopted id (not a freshly generated one) reached the create seam.
    assert "deadbeef" in created_ids
