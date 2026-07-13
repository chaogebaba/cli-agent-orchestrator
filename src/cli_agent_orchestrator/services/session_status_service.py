"""Read-only lifecycle status projection."""

from datetime import datetime, timezone

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    get_session_epoch,
    list_ready_provider_sessions_for_session,
    list_terminals_by_session,
    list_warm_intents,
)


def build_session_status(session_name: str) -> dict:
    terminals = list_terminals_by_session(session_name)
    backend_present = get_backend().session_exists(session_name)
    bases = list_ready_provider_sessions_for_session(session_name)
    intents = list_warm_intents(session_name)
    epoch = get_session_epoch(session_name)
    if not backend_present and not terminals and not bases and not intents and epoch is None:
        raise ValueError("session_missing")

    manifest = None
    manifest_error = None
    if not terminals:
        manifest_error = "no_terminals"
    else:
        try:
            from cli_agent_orchestrator.services.session_manifest_service import build_session_manifest
            manifest = build_session_manifest(session_name)
        except Exception:
            manifest_error = "build_failed"

    return {
        "schema_version": "cao.session-status/v1",
        "session": {"name": session_name},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backend_present": backend_present,
        "manifest": manifest,
        "manifest_error": manifest_error,
        "epoch": (
            {"count": epoch["count"], "last_epoch_at": epoch["last_epoch_at"]}
            if epoch else None
        ),
        "warm_intents": [{
            "intent_id": row["intent_id"],
            "worker_terminal_id": row["worker_terminal_id"],
            "replaces_worker_terminal_id": row["replaces_worker_terminal_id"],
            "profile": row["worker_profile"], "base": row["parent_base_name"],
            "provider": row["provider"], "created_at": row["created_at"],
        } for row in intents],
        "ready_bases": [{
            "base_name": row["name"], "agent_profile": row["agent_profile"],
            "provider": row["provider"], "provider_session_id": row["session_uuid"],
        } for row in bases],
        "quarantined": [{
            "terminal_id": row["id"], "recovery_state": row["recovery_state"],
            "recovery_error": row["recovery_error"], "provider": row["provider"],
            "profile": row["agent_profile"],
        } for row in terminals if row.get("recovery_state") not in (None, "rebound")],
        "ledger": {"available": False, "count": None},
    }
