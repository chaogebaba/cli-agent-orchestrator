"""Narrow fleet projection over the canonical terminal inventory."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from tzlocal import get_localzone

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import list_terminals_by_session
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import status_monitor

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a naive-at-rest DB datetime to UTC (same logic as database._as_utc)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def _compute_init_health(row: dict[str, Any], now: datetime) -> str | None:
    """Derive init_health from init_state and deadline (F124 S1).

    Returns "ready", "launching", "failed", or None (legacy/missing).
    """
    init_state = row.get("init_state")
    if init_state is None:
        return None
    if init_state == "ready":
        return "ready"
    if init_state.startswith("init_failed"):
        return "failed"
    if init_state == "init_pending":
        init_started_at = _as_utc(row.get("init_started_at"))
        deadline_s = row.get("init_deadline_s")
        if init_started_at is None or not isinstance(deadline_s, (int, float)):
            # Malformed pending — fail-closed with structured reason, never throw.
            return "failed"
        elapsed = (now - init_started_at).total_seconds()
        if elapsed >= float(deadline_s):
            return "failed"
        return "launching"
    # Unknown state — omit rather than crash.
    return None


def _depths(rows: list[dict[str, Any]]) -> dict[str, int]:
    by_id = {row["id"]: row for row in rows}
    memo: dict[str, int] = {}

    def depth(terminal_id: str, seen: set[str]) -> int:
        if terminal_id in memo:
            return memo[terminal_id]
        if terminal_id in seen:
            return 0
        parent_id = by_id[terminal_id].get("caller_id")
        if not parent_id:
            value = 0
        elif parent_id not in by_id:
            value = 1
        else:
            value = depth(parent_id, seen | {terminal_id}) + 1
        memo[terminal_id] = value
        return value

    for terminal_id in by_id:
        depth(terminal_id, set())
    return memo


def build_fleet(session_name: str) -> dict[str, Any]:
    rows = list_terminals_by_session(session_name)
    if not rows:
        raise ValueError(f"Session '{session_name}' not found")

    backend = get_backend()
    inventory_reader = getattr(backend, "get_session_windows", None)
    inventory = inventory_reader(session_name) if callable(inventory_reader) else []
    windows = {
        str(item.get("name", item.get("window_name"))): {
            "window_index": item.get("index", item.get("window_index")),
            "window_name": item.get("name", item.get("window_name")),
        }
        for item in inventory
    }
    has_native_inventory = callable(inventory_reader)
    by_id = {row["id"]: row for row in rows}
    depths = _depths(rows)
    now = datetime.now(timezone.utc)
    projected: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: item["id"]):
        parent_id = row.get("caller_id")
        parent = by_id.get(parent_id) if parent_id else None
        parent_dead = bool(
            parent
            and has_native_inventory
            and parent["tmux_window"] not in windows
            and parent.get("recovery_state") != "fallback_ready"
        )
        orphan = bool(parent_id and (parent is None or parent_dead))
        observation = status_monitor.get_boundary_observation(row["id"])
        status = observation.status
        if row.get("recovery_state") not in (None, "rebound"):
            status = TerminalStatus.ERROR
        if has_native_inventory and row["tmux_window"] not in windows:
            status = TerminalStatus.ERROR
        # F124 S1: compute init_health; failed health overrides status to ERROR.
        init_health = _compute_init_health(row, now)
        if init_health == "failed":
            status = TerminalStatus.ERROR
        last_active = row.get("last_active")
        if last_active is not None:
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=get_localzone(), fold=0).astimezone(
                    timezone.utc
                )
            else:
                last_active = last_active.astimezone(timezone.utc)
            since_last_input = max(0.0, (now - last_active).total_seconds())
        else:
            since_last_input = None
        window = windows.get(row["tmux_window"], {})
        projected.append(
            {
                "id": row["id"],
                "profile": row.get("agent_profile"),
                "provider": row.get("provider"),
                "window_index": window.get("window_index"),
                "window_name": (
                    (window.get("window_name") if window else row["tmux_window"])
                    if has_native_inventory
                    else None
                ),
                "parent_id": parent_id,
                "depth": depths[row["id"]],
                "orphan": orphan,
                "status": status.value,
                "init_state": row.get("init_state"),
                "init_health": init_health,
                "since_last_input": since_last_input,
                "lifecycle": row.get("lifecycle", "ephemeral"),
                "resolved_model": row.get("resolved_model"),
                "reparented_from": row.get("reparented_from"),
            }
        )
    return {"session_name": session_name, "terminals": projected}
