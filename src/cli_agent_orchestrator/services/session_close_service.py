"""Mechanical close settlement over the shared leased teardown seam."""

from __future__ import annotations

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    delete_session_epoch,
    delete_warm_intents_for_session,
    get_ready_provider_session_by_source_terminal,
    get_terminal_metadata,
    list_ready_provider_sessions_for_session,
    list_terminals_by_session,
    list_warm_intents,
    retire_provider_session,
)
from cli_agent_orchestrator.services.rebind_lease import acquire_rebind_lease, release_rebind_lease
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile


def close_session(session_name: str, *, keep_bases: bool = False, force: bool = False,
                  registry=None) -> dict:
    from cli_agent_orchestrator.services import terminal_service

    terminals = list_terminals_by_session(session_name)
    registrations = list_ready_provider_sessions_for_session(session_name)
    source_snapshot = {
        row.get("source_terminal_id"): (
            get_terminal_metadata(row["source_terminal_id"])
            if row.get("source_terminal_id") else None
        )
        for row in registrations
    }
    scoped_base_sources = {row.get("source_terminal_id") for row in registrations}
    intents_before = list_warm_intents(session_name)
    for terminal in terminals:
        owner = get_ready_provider_session_by_source_terminal(terminal["id"])
        if owner is not None and terminal["id"] not in scoped_base_sources and not force:
            raise PermissionError(
                f"ready base '{owner['name']}' is not scoped to session {session_name}"
            )
        try:
            profile = load_agent_profile(terminal.get("agent_profile"))
        except (FileNotFoundError, TypeError):
            profile = None
        if profile is not None and profile.protected is True and not force:
            raise PermissionError(f"protected profile '{profile.name}' requires force")

    leases = []
    try:
        for terminal in sorted(terminals, key=lambda row: row["id"]):
            token = acquire_rebind_lease(terminal["id"])
            if token is None:
                raise RuntimeError("rebind_in_progress")
            leases.append(token)
        tokens = {token.terminal_id: token for token in leases}
        terminal_outcomes = []
        delete_by_id = {}
        removed_stage1 = 0
        intent_errors = []
        for terminal in terminals:
            try:
                mechanical = terminal_service._delete_terminal_under_lease(
                    terminal["id"], tokens[terminal["id"]], registry=registry,
                    preserve_warm_intent=keep_bases,
                )
                deleted = bool(mechanical["terminal_deleted"])
                status = "deleted" if deleted else "delete_failed"
                if mechanical.get("intent_deleted"):
                    removed_stage1 += 1
                if mechanical.get("intent_error"):
                    intent_errors.append(mechanical["intent_error"])
            except Exception as exc:
                deleted = False
                status = "delete_failed"
                mechanical = {"intent_deleted": False, "intent_error": str(exc)}
                intent_errors.append(str(exc))
            delete_by_id[terminal["id"]] = deleted
            terminal_outcomes.append({"terminal_id": terminal["id"], "status": status,
                                      "intent_deleted": bool(mechanical.get("intent_deleted"))})

        backend = get_backend()
        try:
            from cli_agent_orchestrator.services.session_service import finalize_session
            finalize_session(session_name, registry, backend=backend)
        except Exception:
            pass
        session_closed = (not backend.session_exists(session_name)
                          and not list_terminals_by_session(session_name))

        base_outcomes = []
        for registration in registrations:
            source_id = registration.get("source_terminal_id")
            source = source_snapshot.get(source_id)
            if source is not None and source.get("tmux_session") != session_name:
                settlement = "skipped_other_session"
            elif source_id in delete_by_id:
                if not delete_by_id[source_id]:
                    settlement = "source_not_deleted"
                elif keep_bases:
                    settlement = "kept"
                else:
                    try:
                        settlement = "retired" if retire_provider_session(registration["name"]) else "retire_failed"
                    except Exception:
                        settlement = "retire_failed"
            elif source is None:
                if keep_bases:
                    settlement = "kept"
                else:
                    try:
                        retired = retire_provider_session(registration["name"])
                        settlement = "source_missing" if retired else "retire_failed"
                    except Exception:
                        settlement = "retire_failed"
            else:
                settlement = "source_not_deleted"
            base_outcomes.append({"base": registration["name"], "status": settlement})

        removed_stage2 = 0
        if session_closed:
            if not keep_bases:
                try:
                    removed_stage2 = delete_warm_intents_for_session(session_name)
                except Exception as exc:
                    intent_errors.append(str(exc))
            delete_session_epoch(session_name)
        retained = len(list_warm_intents(session_name))
        return {
            "schema_version": "cao.session-close/v1", "session": session_name,
            "session_closed": session_closed, "terminals": terminal_outcomes,
            "bases": base_outcomes,
            "intents": {"removed": removed_stage1 + removed_stage2,
                        "retained": retained, "errors": intent_errors},
        }
    finally:
        for token in reversed(leases):
            try:
                release_rebind_lease(token)
            except Exception:
                pass
