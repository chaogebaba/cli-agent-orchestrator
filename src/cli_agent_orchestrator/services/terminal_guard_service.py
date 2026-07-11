"""API-boundary policy preflights for protected terminal input and teardown.

All public HTTP dispatch and destructive endpoints call these guards. Trusted
internal lifecycle owners intentionally bypass them: ``flow_service`` and
``script_runner`` reclaim terminals they created, ``herdr_inbox_service`` cleans
up provider workspaces, and ``agent_step`` performs ownership-scoped teardown.
Those are not user-facing authorization surfaces. ``/key`` is also intentionally
outside this policy: it is an interactive control where interrupts remain allowed.
"""

from cli_agent_orchestrator.clients.database import (
    get_ready_provider_session_by_source_terminal,
    get_terminal_metadata,
)
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile


class TerminalProtectionError(ValueError):
    """Raised before any mutation when a protected terminal is targeted."""


def require_input_allowed(terminal_id: str, *, refresh_ingest: bool = False) -> None:
    if refresh_ingest:
        return
    ready_base = get_ready_provider_session_by_source_terminal(terminal_id)
    if ready_base is not None:
        raise TerminalProtectionError(
            f"terminal owns ready base '{ready_base['name']}'; only refresh-ingest "
            "dispatches allowed — pass refresh_ingest=true"
        )


def require_delete_allowed(terminal_id: str, *, force: bool = False) -> None:
    if force:
        return
    ready_base = get_ready_provider_session_by_source_terminal(terminal_id)
    if ready_base is not None:
        raise TerminalProtectionError(
            f"Terminal {terminal_id} owns ready base '{ready_base['name']}' and is protected; "
            "pass force=true to delete it"
        )
    metadata = get_terminal_metadata(terminal_id)
    profile_name = metadata.get("agent_profile") if metadata else None
    if not profile_name:
        return
    try:
        profile = load_agent_profile(profile_name)
    except FileNotFoundError:
        return
    if profile.protected is True:
        raise TerminalProtectionError(
            f"Terminal {terminal_id} uses protected profile '{profile_name}'; "
            "pass force=true to delete it"
        )
