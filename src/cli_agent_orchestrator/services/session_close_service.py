"""Mechanical close settlement over the shared leased teardown seam."""

from __future__ import annotations

from typing import Any

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
from cli_agent_orchestrator.utils.persona_context import (
    PersonaRetentionIntent,
    retained_persona_destination,
)


def close_session(
    session_name: str, *, keep_bases: bool = False, force: bool = False, registry=None
) -> dict:
    from cli_agent_orchestrator.services import terminal_service

    leases = []
    lifecycle_lease = None
    try:
        from cli_agent_orchestrator.services.session_lifecycle_lease import (
            acquire_session_lifecycle_exclusive,
        )

        terminal_service.quiesce_deferred_session_sync(session_name)
        lifecycle_lease = acquire_session_lifecycle_exclusive(session_name)
        if lifecycle_lease is None:
            raise RuntimeError("resume_in_progress")
        terminals = list_terminals_by_session(session_name)
        registrations = list_ready_provider_sessions_for_session(session_name)
        retention_by_terminal: dict[str, PersonaRetentionIntent] = {}
        retention_setup_errors: list[str] = []
        if keep_bases:
            groups: dict[str, list[dict[str, Any]]] = {}
            for row in registrations:
                if (
                    row.get("kind", "base") == "base"
                    and row.get("provider") == "codex"
                    and row.get("source_terminal_id")
                ):
                    groups.setdefault(row["session_uuid"], []).append(row)
            for session_uuid, members in groups.items():
                try:
                    intent = PersonaRetentionIntent(
                        session_uuid=session_uuid,
                        destination=retained_persona_destination(session_uuid),
                        member_row_ids=tuple(sorted(int(row["id"]) for row in members)),
                    )
                except Exception as exc:
                    retention_setup_errors.append(str(exc))
                    continue
                for row in members:
                    retention_by_terminal[str(row["source_terminal_id"])] = intent
        source_snapshot = {
            row.get("source_terminal_id"): (
                get_terminal_metadata(row["source_terminal_id"])
                if row.get("source_terminal_id")
                else None
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
        for terminal in sorted(terminals, key=lambda row: row["id"]):
            token = acquire_rebind_lease(terminal["id"])
            if token is None:
                raise RuntimeError("rebind_in_progress")
            leases.append(token)
        tokens = {token.terminal_id: token for token in leases}
        terminal_service.preflight_session_teardown(terminals)
        terminal_outcomes = []
        delete_by_id = {}
        removed_stage1 = 0
        intent_errors = []
        intent_errors.extend(retention_setup_errors)
        for terminal in terminals:
            try:
                mechanical = terminal_service._delete_terminal_under_lease(
                    terminal["id"],
                    tokens[terminal["id"]],
                    registry=registry,
                    preserve_warm_intent=keep_bases,
                    persona_retention_intent=retention_by_terminal.get(terminal["id"]),
                )
                deleted = bool(mechanical["terminal_deleted"])
                status = "deleted" if deleted else "delete_failed"
                if mechanical.get("intent_deleted"):
                    removed_stage1 += 1
                if mechanical.get("intent_error"):
                    intent_errors.append(mechanical["intent_error"])
                if mechanical.get("persona_retention_error"):
                    intent_errors.append(mechanical["persona_retention_error"])
            except Exception as exc:
                if str(exc) == "resume_in_progress":
                    raise
                deleted = False
                status = "delete_failed"
                mechanical = {"intent_deleted": False, "intent_error": str(exc)}
                intent_errors.append(str(exc))
            delete_by_id[terminal["id"]] = deleted
            terminal_outcomes.append(
                {
                    "terminal_id": terminal["id"],
                    "status": status,
                    "intent_deleted": bool(mechanical.get("intent_deleted")),
                }
            )

        backend = get_backend()
        try:
            from cli_agent_orchestrator.services.session_service import finalize_session

            finalize_session(session_name, registry, backend=backend)
        except Exception:
            pass
        session_closed = not backend.session_exists(session_name) and not list_terminals_by_session(
            session_name
        )

        base_outcomes = []
        for registration in registrations:
            source_id = registration.get("source_terminal_id")
            source = source_snapshot.get(source_id)
            if source is not None and source.get("tmux_session") != session_name:
                settlement = "skipped_other_session"
            elif source_id in delete_by_id:
                if not delete_by_id[source_id]:
                    settlement = "source_not_deleted"
                elif keep_bases and registration.get("kind", "base") == "base":
                    settlement = "kept"
                else:
                    try:
                        settlement = (
                            "retired"
                            if retire_provider_session(registration["name"])
                            else "retire_failed"
                        )
                    except Exception:
                        settlement = "retire_failed"
            elif source is None:
                if keep_bases and registration.get("kind", "base") == "base":
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
            "schema_version": "cao.session-close/v1",
            "session": session_name,
            "session_closed": session_closed,
            "terminals": terminal_outcomes,
            "bases": base_outcomes,
            "intents": {
                "removed": removed_stage1 + removed_stage2,
                "retained": retained,
                "errors": intent_errors,
            },
        }
    finally:
        for token in reversed(leases):
            try:
                release_rebind_lease(token)
            except Exception:
                pass
        if lifecycle_lease is not None:
            from cli_agent_orchestrator.services.session_lifecycle_lease import (
                release_session_lifecycle_lease,
            )

            release_session_lifecycle_lease(lifecycle_lease)
