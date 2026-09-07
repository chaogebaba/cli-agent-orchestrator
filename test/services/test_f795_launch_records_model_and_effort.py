"""F795 (#652): every terminal-creation path must record the effective model
and reasoning effort — including the SYNCHRONOUS one that ``cao launch`` uses.

Observed: the supervisor seat's fleet row read ``chao_supervisor claude - -``
and ``GET /terminals/d1203cc7`` returned ``resolved_model: null,
reasoning_effort: null``, while worker rows created by ``assign`` carried both.
fleet_service reads the two columns straight off the terminal row, and only the
DEFERRED init path ever wrote them (_schedule_deferred_init._run). ``cao launch``
has no initial_message, so it never defers and both columns stayed NULL — even
though providers.toml pins the profile's model and effort and the provider had
already resolved both during build_command.

These tests drive the REAL ``create_terminal`` synchronous branch with only its
resource dependencies stubbed, and assert on the persistence calls the row is
built from.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.session_lifecycle_lease import (
    SessionLifecycleLeaseToken,
)

RESOLVED_MODEL = "claude-fable-5-1"
RESOLVED_EFFORT = "medium"


def _launch_seam(*, resolved_model, resolved_effort, recorded: dict):
    """Patch create_terminal's resource deps, keeping the REAL sync branch."""
    backend = MagicMock()
    backend.session_exists.return_value = False
    backend.create_window.side_effect = lambda session, window, *a, **kw: window
    backend.supports_event_inbox.return_value = False
    backend.set_window_parent = None

    provider = AsyncMock()
    provider.initialize.return_value = True
    provider.shell_baseline = None
    # What build_command resolved through the one provider chain
    # (providers.toml [<provider>.profiles.<name>] > profile field > CLI default).
    provider.resolved_model = resolved_model
    provider.resolved_reasoning_effort = resolved_effort

    ids = iter(f"seat{i:04d}" for i in range(1, 1000))

    return (
        patch.multiple(
            ts,
            _resolve_worker_terminal_cap=lambda *a, **k: 0,
            list_terminals_by_session=lambda s: [],
            db_create_terminal=lambda terminal_id, *a, **kw: {"id": terminal_id},
            delete_terminals_by_session=MagicMock(),
            generate_terminal_id=lambda: next(ids),
            generate_window_name=lambda profile, tid: f"{profile}-{tid}",
            provider_manager=MagicMock(create_provider=MagicMock(return_value=provider)),
            fifo_manager=MagicMock(),
            _schedule_deferred_init=MagicMock(),
            require_provider_admitted=lambda provider: None,
            load_agent_profile=lambda name: AgentProfile(
                name="chao_supervisor", description="supervisor"
            ),
            get_provider_class=lambda name: type(
                "Cap",
                (),
                {"supports_seed_resume_identity": False, "has_process_child": False},
            ),
            _persist_provider_runtime_identity=lambda *a, **kw: None,
            update_terminal_shell_command=MagicMock(),
            update_terminal_resolved_model=lambda tid, value: recorded.__setitem__(
                "model", (tid, value)
            ),
            update_terminal_reasoning_effort=lambda tid, value: recorded.__setitem__(
                "effort", (tid, value)
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


async def _launch(*, resolved_model, resolved_effort, recorded: dict):
    """The `cao launch` shape: new session, no caller, no initial_message —
    so create_terminal takes its SYNCHRONOUS branch."""
    seam, backend = _launch_seam(
        resolved_model=resolved_model, resolved_effort=resolved_effort, recorded=recorded
    )
    l1, l2 = _lease_patches()
    with seam, l1, l2, patch("cli_agent_orchestrator.backends.registry._backend", backend):
        return await ts.create_terminal(
            provider="mock_cli",
            agent_profile="chao_supervisor",
            session_name="cao-f795",
            new_session=True,
            caller_id=None,
        )


@pytest.mark.asyncio
async def test_launch_created_row_has_both_fields_set() -> None:
    """MUTANT SENTINEL. Reverting the sync-path persistence leaves `recorded`
    empty — exactly the observed `model - / effort -` supervisor row."""
    recorded: dict = {}

    terminal = await _launch(
        resolved_model=RESOLVED_MODEL, resolved_effort=RESOLVED_EFFORT, recorded=recorded
    )

    assert recorded.get("model") == (terminal.id, RESOLVED_MODEL)
    assert recorded.get("effort") == (terminal.id, RESOLVED_EFFORT)


@pytest.mark.asyncio
async def test_returned_terminal_object_echoes_both_fields() -> None:
    """The API response for the launch itself must not report null either."""
    recorded: dict = {}

    terminal = await _launch(
        resolved_model=RESOLVED_MODEL, resolved_effort=RESOLVED_EFFORT, recorded=recorded
    )

    assert terminal.resolved_model == RESOLVED_MODEL
    assert terminal.reasoning_effort == RESOLVED_EFFORT


@pytest.mark.asyncio
async def test_provider_without_the_knob_is_not_invented() -> None:
    """Honest unknown: a provider that resolves nothing leaves the columns
    unwritten so the fleet renders "-", rather than a fabricated value."""
    recorded: dict = {}

    terminal = await _launch(resolved_model=None, resolved_effort=None, recorded=recorded)

    assert recorded == {}
    assert terminal.resolved_model is None
    assert terminal.reasoning_effort is None
