"""Canonical live session manifest and inventory-only Markdown renderer."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import frontmatter

from cli_agent_orchestrator.clients.database import list_terminals_by_session
from cli_agent_orchestrator.services.fork_context_service import list_bases
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.verification_service import deployment_status
from cli_agent_orchestrator.services.workflow_spec_service import list_workflows
from cli_agent_orchestrator.utils.agent_profiles import (
    list_agent_profiles, parse_agent_profile_text, read_agent_profile_source,
)
from cli_agent_orchestrator.utils.skills import list_skills

SCHEMA_VERSION = "cao.session-manifest/v1"


def _charter_projection(name: str) -> dict[str, Any]:
    raw = read_agent_profile_source(name)
    parsed = frontmatter.loads(raw)
    profile = parse_agent_profile_text(raw, name)
    body = parsed.content.strip()
    digest = (profile.description or "").strip()
    if not digest:
        digest = next((line.strip() for line in body.splitlines() if line.strip()), "")
    return {
        "name": profile.name, "description": profile.description,
        "provider": profile.provider, "role": profile.role,
        "skills": profile.skills or [], "message_contract": profile.messageContract,
        "charter_digest": digest[:140], "charter": body,
        "session_brief": profile.sessionBrief,
    }


def build_session_manifest(session_name: str, terminal_id: str | None = None) -> dict[str, Any]:
    sections = {
        name: "ok"
        for name in (
            "profiles", "ready_bases", "skills", "workflows", "terminals", "activation"
        )
    }
    sections.update({"tools": "not_collected", "ledger": "not_collected"})
    errors: list[dict[str, str]] = []

    def collect(section: str, fn: Callable[[], Any], fallback: Any) -> Any:
        try:
            return fn()
        except Exception as exc:
            sections[section] = "error"
            errors.append({"section": section, "code": type(exc).__name__, "message": str(exc)})
            return fallback

    def profiles() -> list[dict[str, Any]]:
        rows = []
        for item in list_agent_profiles():
            row = _charter_projection(item["name"])
            row.update(source=item.get("source"), duplicated_in=item.get("duplicated_in", []))
            rows.append(row)
        return sorted(rows, key=lambda r: r["name"])

    profile_rows = collect("profiles", profiles, [])
    roles = {row["name"]: row.get("role") for row in profile_rows}
    raw_terminals = collect("terminals", lambda: list_terminals_by_session(session_name), [])
    if not raw_terminals and sections["terminals"] == "ok":
        raise ValueError(f"Session '{session_name}' not found")

    def terminals() -> list[dict[str, Any]]:
        rows = []
        from cli_agent_orchestrator.services.terminal_service import get_working_directory
        for item in raw_terminals:
            role = roles.get(item.get("agent_profile"))
            started_at = auth_mtime = None
            auth_staleness = "unknown"
            try:
                from cli_agent_orchestrator.providers.manager import provider_manager
                from cli_agent_orchestrator.services.fork_context_service import pane_pid
                provider = provider_manager.get_provider(item["id"])
                pid = pane_pid(item["tmux_session"], item["tmux_window"])
                started_at = provider.provider_process_started_at(pid)
                auth_path = provider.auth_state_path()
                if auth_path and auth_path.is_file():
                    auth_mtime = auth_path.stat().st_mtime
                    if started_at is not None:
                        auth_staleness = "stale" if started_at < auth_mtime else "current"
            except Exception:
                pass
            rows.append({
                "id": item["id"], "profile": item.get("agent_profile"),
                "provider": item.get("provider"),
                "status": status_monitor.get_status(item["id"]).value,
                "caller_id": item.get("caller_id"),
                "cwd": get_working_directory(item["id"]),
                "kind": "supervisor" if role == "supervisor" else ("worker" if role in {"developer", "reviewer", "worker"} else "unknown"),
                "recovery_state": item.get("recovery_state"),
                "recovery_error": item.get("recovery_error"),
                "fallback_terminal_id": item.get("fallback_terminal_id"),
                "provider_process_started_at": started_at,
                "auth_state_mtime": auth_mtime,
                "auth_staleness": auth_staleness,
            })
        return sorted(rows, key=lambda r: r["id"])

    terminal_rows = collect("terminals", terminals, [])
    base_rows = collect("ready_bases", lambda: sorted([{
        "name": b["name"], "provider": b["provider"], "profile": b.get("agent_profile"),
        "source_terminal_id": b.get("source_terminal_id"), "cwd": b.get("cwd"),
        "git_sha": b.get("git_sha"), "staleness_count": b.get("staleness_count", 0),
        "status": b.get("status"), "kind": b.get("kind", "base"),
        "updated_at": b.get("updated_at"),
    } for b in list_bases()], key=lambda r: r["name"]), [])
    skill_rows = collect("skills", lambda: [{"name": s.name, "description": s.description} for s in list_skills()], [])
    workflow_rows = collect("workflows", lambda: sorted([{"name": w.name, "description": w.description, "source": w.source_path} for w in list_workflows()], key=lambda r: r["name"]), [])
    source_root = os.environ.get("CAO_SOURCE_REPO")
    if source_root:
        activation = collect(
            "activation", lambda: deployment_status(Path(source_root)),
            {"cli_path": "unknown", "differing_files": None, "server": "unknown", "source_root": source_root},
        )
    else:
        activation = {"cli_path": "unknown", "differing_files": None, "server": "unknown", "source_root": None}
        sections["activation"] = "error"
        errors.append({"section": "activation", "code": "source_root_unconfigured", "message": "CAO_SOURCE_REPO is not set"})
    from cli_agent_orchestrator.clients.database import get_session_epoch
    epoch_row = get_session_epoch(session_name)
    supervisor_ids = sorted(
        item["id"]
        for item in raw_terminals
        if roles.get(item.get("agent_profile")) == "supervisor"
    )
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
        "complete": all(state == "ok" for state in sections.values()),
        "errors": errors, "sections": sections,
        "session": {"name": session_name,
                    "supervisors": supervisor_ids,
                    "supervisor_terminal_id": supervisor_ids[0] if len(supervisor_ids) == 1 else None,
                    "epoch": epoch_row["count"] if epoch_row else 0,
                    "epoch_started_at": epoch_row["last_epoch_at"] if epoch_row else None},
        "profiles": profile_rows, "ready_bases": base_rows, "skills": skill_rows,
        "workflows": workflow_rows, "tools": None, "terminals": terminal_rows,
        "ledger": {"pending_rows": None}, "activation": activation,
    }


def render_session_brief(manifest: dict[str, Any], thin: bool = False) -> str:
    lines = ["## CAO Live Session Inventory", f"generated_at: {manifest['generated_at']}", f"complete: {str(manifest['complete']).lower()}"]
    names = ", ".join(p["name"] for p in manifest["profiles"])
    if thin:
        return "\n".join(lines + [f"profiles: {names}", "run `cao session manifest --brief` for full"])
    lines += [f"session: {manifest['session']['name']}", "", "### Profiles"]
    for p in manifest["profiles"]:
        lines.append(f"- {p['name']} — role={p.get('role') or 'unknown'}, provider={p.get('provider') or 'default'}, skills={','.join(p['skills']) or '(none)'}; {p['charter_digest']}")
    lines += ["", "### Ready bases"]
    lines += [f"- {b['name']} — {b['provider']}/{b.get('profile')}, stale={b['staleness_count']}" for b in manifest["ready_bases"]] or ["- (none)"]
    lines += ["", "### Skills", *(f"- {s['name']} — {s['description']}" for s in manifest["skills"])]
    lines += ["", "### Workflows", *(f"- {w['name']} — {w['description']}" for w in manifest["workflows"])]
    lines += ["", "### Terminals", *(f"- {t['id']} — {t.get('profile')} ({t.get('provider')}, {t.get('status')}, {t.get('kind')})" for t in manifest["terminals"])]
    a = manifest["activation"]
    lines += ["", "### Activation", f"- cli_path={a.get('cli_path')}, differing_files={a.get('differing_files')}, server={a.get('server')}, source_root={a.get('source_root')}"]
    section_states = manifest.get("sections")
    if isinstance(section_states, dict):
        incomplete = [
            (name, state)
            for name, state in section_states.items()
            if state != "ok"
        ]
    else:
        # Additive-v1 compatibility for callers holding a pre-lattice snapshot.
        incomplete = [(error["section"], "error") for error in manifest["errors"]]
    if incomplete:
        error_codes = {error["section"]: error["code"] for error in manifest["errors"]}
        lines += ["", "### Incomplete sections"]
        for name, state in incomplete:
            suffix = f" ({error_codes[name]})" if state == "error" and name in error_codes else ""
            lines.append(f"- {name}: {state}{suffix}")
    return "\n".join(lines)


def core_sections_complete(manifest: dict[str, Any]) -> bool:
    return all(manifest["sections"].get(name) == "ok" for name in ("profiles", "skills"))
