"""Epoch recovery of registered bases into an existing CAO session."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    get_provider_session_history,
    get_ready_provider_session,
    increment_session_epoch,
    list_ready_provider_sessions_for_session,
    list_warm_intents,
)
from cli_agent_orchestrator.models.terminal import ForkContext
from cli_agent_orchestrator.providers.manager import get_provider_class
from cli_agent_orchestrator.services.fork_context_service import mark_ready, staleness
from cli_agent_orchestrator.services.epoch_recovery_lease import (
    acquire_epoch_recovery_lease, release_epoch_recovery_lease,
)
from cli_agent_orchestrator.services.rebind_lease import acquire_rebind_lease, release_rebind_lease
from cli_agent_orchestrator.services.terminal_service import create_terminal, provider_session_owner
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile, resolve_provider
from cli_agent_orchestrator.utils.terminal import generate_terminal_id


def _result(base, status, terminal_id=None, error_code=None, unscoped=False):
    retryable = status in {"resume_failed", "skipped_busy"} and error_code not in {
        "rollback_kill_uncertain", "quarantine_persist_failed",
    }
    row = {"base": base, "status": status, "terminal_id": terminal_id,
           "error_code": error_code, "retryable": retryable}
    if unscoped:
        row["unscoped_registration"] = True
    return row


def _artifact_exists(row) -> bool:
    if row["provider"] == "codex":
        return any(row["session_uuid"] in path.name for path in
                   (Path.home() / ".codex" / "sessions").glob("**/rollout-*.jsonl"))
    if row["provider"] == "grok_cli":
        return (Path.home() / ".grok" / "sessions" / quote(row["cwd"], safe="") /
                row["session_uuid"]).exists()
    return False


def _normalize_creation_error(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, TimeoutError):
        return "initialize_timeout"
    for code in (
        "window_create_failed", "fifo_create_failed", "db_publish_failed",
        "context_build_failed", "provider_construct_failed", "initialize_failed",
        "session_capture_ambiguous", "session_capture_mismatch", "artifact_invalid",
        "identity_persist_failed", "herdr_register_failed",
        "rollback_kill_uncertain",
        "quarantine_persist_failed",
    ):
        if code in message:
            return code
    if "session_capture_ambiguous" in message:
        return "session_capture_ambiguous"
    if "session_capture_mismatch" in message:
        return "session_capture_mismatch"
    if message in {"terminal_metadata_missing", "shell_baseline_unavailable",
                   "terminal_identity_persist_failed"}:
        return "identity_persist_failed"
    if message.startswith("session_artifact_"):
        return "artifact_invalid"
    return "initialize_failed"


def _preflight(row, session_name):
    if row.get("session_name") is not None and row["session_name"] != session_name:
        return _result(row["name"], "wrong_session")
    if not _artifact_exists(row):
        return _result(row["name"], "artifact_missing",
                       unscoped=row.get("session_name") is None)
    if provider_session_owner(row["session_uuid"])["state"] != "gone":
        return _result(row["name"], "skipped_live_owner",
                       unscoped=row.get("session_name") is None)
    try:
        load_agent_profile(row["agent_profile"])
    except Exception:
        return _result(row["name"], "profile_unresolvable",
                       error_code="profile_load_failed",
                       unscoped=row.get("session_name") is None)
    provider = resolve_provider(row["agent_profile"], row["provider"])
    if provider != row["provider"]:
        return _result(row["name"], "profile_unresolvable",
                       error_code="provider_mismatch",
                       unscoped=row.get("session_name") is None)
    try:
        supports = get_provider_class(provider).supports_fork_context
    except ValueError:
        supports = False
    if not supports:
        return _result(row["name"], "profile_unresolvable",
                       error_code="provider_lacks_fork_capability",
                       unscoped=row.get("session_name") is None)
    return None


async def _recover_row(row, session_name):
    failed = _preflight(row, session_name)
    if failed:
        return failed, None
    terminal_id = generate_terminal_id()
    lease = acquire_rebind_lease(terminal_id)
    if lease is None:
        return _result(row["name"], "skipped_busy", error_code="rebind_in_progress",
                       unscoped=row.get("session_name") is None), None
    try:
        context = ForkContext(mode="resume", session_uuid=row["session_uuid"],
                              base_name=row["name"], provider=row["provider"],
                              initial_preamble="")
        try:
            terminal = await create_terminal(
                provider=row["provider"], agent_profile=row["agent_profile"],
                session_name=session_name, new_session=False, working_directory=row["cwd"],
                defer_init=False, fork_context=context, terminal_id=terminal_id,
                lease_token=lease, strict_backend_registration=True,
            )
        except Exception as exc:
            return _result(row["name"], "resume_failed", terminal_id,
                           _normalize_creation_error(exc),
                           row.get("session_name") is None), None
        remark_error = None
        try:
            mark_ready(terminal.id, row["name"], row.get("summary"))
        except Exception:
            remark_error = "remark_failed"
        changed, _ = staleness(row)
        source = {"base": row["name"], "terminal_id": terminal.id,
                  "status": "resumed", "error_code": remark_error,
                  "staleness": None if changed is None else len(changed),
                  "stale_registration": remark_error == "remark_failed"}
        return _result(row["name"], "resumed", terminal.id, remark_error,
                       row.get("session_name") is None), source
    finally:
        release_rebind_lease(lease)


async def recover_epoch(session_name: str, base_names: list[str] | None = None) -> dict:
    if not get_backend().session_exists(session_name):
        raise ValueError("session_missing")
    started = datetime.now(timezone.utc).isoformat()
    selected = []
    pre_results = []
    if base_names:
        for name in sorted(set(base_names)):
            row = get_ready_provider_session(name)
            if row is None:
                historical = get_provider_session_history(name)
                if historical is None:
                    pre_results.append(_result(name, "not_found"))
                else:
                    pre_results.append(_result(name, "not_ready", error_code=historical["status"]))
            else:
                selected.append(row)
    else:
        selected = list_ready_provider_sessions_for_session(session_name)

    results = list(pre_results)
    sources = []
    resumed_names = set()
    for row in sorted(selected, key=lambda item: item["name"]):
        recovery_lease = acquire_epoch_recovery_lease(session_name, row["name"])
        if recovery_lease is None:
            results.append(_result(row["name"], "skipped_busy", error_code="rebind_in_progress",
                                   unscoped=row.get("session_name") is None))
            continue
        try:
            result, source = await _recover_row(row, session_name)
            results.append(result)
            if result["status"] == "resumed":
                resumed_names.add(row["name"])
            if source:
                sources.append(source)
        finally:
            release_epoch_recovery_lease(recovery_lease)

    epoch = increment_session_epoch(session_name) if resumed_names else None
    result_by_name = {row["base"]: row for row in results}
    candidates = []
    for intent in list_warm_intents(session_name):
        parent = result_by_name.get(intent["parent_base_name"])
        state = "not_selected" if parent is None else (
            "resumed" if parent["status"] == "resumed" else
            parent["status"] if parent["status"] in {"not_found", "not_ready"} else "failed")
        base = get_ready_provider_session(intent["parent_base_name"])
        stale = None
        if base:
            changed, _ = staleness(base)
            stale = None if changed is None else len(changed)
        candidates.append({
            "intent_id": intent["intent_id"],
            "worker_terminal_id": intent["worker_terminal_id"],
            "replaces_worker_terminal_id": intent["replaces_worker_terminal_id"],
            "profile": intent["worker_profile"], "base": intent["parent_base_name"],
            "provider": intent["provider"], "base_state": state,
            "base_resumed": state == "resumed", "base_staleness": stale,
        })
    manifest = None
    manifest_error = None
    try:
        from cli_agent_orchestrator.services.session_manifest_service import build_session_manifest
        manifest = build_session_manifest(session_name)
    except Exception as exc:
        manifest_error = str(exc)
    return {
        "schema_version": "cao.session-recover/v1", "session": session_name,
        "reason": "epoch", "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(), "results": results,
        "manifest": manifest, "manifest_error": manifest_error,
        "fork_sources": sources, "respawn_candidates": candidates, "epoch": epoch,
    }
