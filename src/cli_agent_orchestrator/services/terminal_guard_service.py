"""API-boundary policy preflights for protected terminal input and teardown.

All public HTTP dispatch and destructive endpoints call these guards. Trusted
internal lifecycle owners intentionally bypass them: ``flow_service`` and
``script_runner`` reclaim terminals they created, ``herdr_inbox_service`` cleans
up provider workspaces, and ``agent_step`` performs ownership-scoped teardown.
Those are not user-facing authorization surfaces. ``/key`` is also intentionally
outside this policy: it is an interactive control where interrupts remain allowed.
"""

from dataclasses import dataclass

from cli_agent_orchestrator.clients.database import (
    get_ready_provider_session_by_source_terminal,
    get_terminal_metadata,
)
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile


class TerminalProtectionError(ValueError):
    """Raised before any mutation when a protected terminal is targeted."""


@dataclass(frozen=True)
class DeletionClassification:
    allowed: bool
    reason: str | None = None


def classify_deletion(terminal_id: str, *, force: bool = False) -> DeletionClassification:
    """Classify terminal deletion without raising so cascades can skip descendants."""
    if force:
        return DeletionClassification(True)
    ready_base = get_ready_provider_session_by_source_terminal(terminal_id)
    if ready_base is not None:
        return DeletionClassification(False, f"ready_base:{ready_base['name']}")
    metadata = get_terminal_metadata(terminal_id)
    if metadata and metadata.get("lifecycle") == "sticky":
        return DeletionClassification(False, "sticky")
    profile_name = metadata.get("agent_profile") if metadata else None
    if not profile_name:
        return DeletionClassification(True)
    try:
        profile = load_agent_profile(profile_name)
    except FileNotFoundError:
        return DeletionClassification(True)
    if profile.protected is True:
        return DeletionClassification(False, f"protected_profile:{profile_name}")
    return DeletionClassification(True)


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
    classification = classify_deletion(terminal_id, force=force)
    if classification.allowed:
        return
    if classification.reason and classification.reason.startswith("ready_base:"):
        name = classification.reason.split(":", 1)[1]
        raise TerminalProtectionError(
            f"Terminal {terminal_id} owns ready base '{name}' and is protected; "
            "pass force=true to delete it"
        )
    if classification.reason and classification.reason.startswith("protected_profile:"):
        profile_name = classification.reason.split(":", 1)[1]
        raise TerminalProtectionError(
            f"Terminal {terminal_id} uses protected profile '{profile_name}'; "
            "pass force=true to delete it"
        )
    raise TerminalProtectionError(
        f"Terminal {terminal_id} is protected ({classification.reason}); pass force=true to delete it"
    )
