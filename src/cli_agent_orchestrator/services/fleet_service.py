"""Narrow fleet projection over the canonical terminal inventory."""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# WP-ARCH phase 1 (F725 #581) hook point 2b — the three fleet ERROR overrides.
from cli_agent_orchestrator.adapters.truth import legacy_egress as _wt_legacy_egress
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import list_terminals_by_session
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.utils.provider_plane import provider_home

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a naive-at-rest DB datetime to UTC (same logic as database._as_utc)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def _fleet_position(column_value: Any, agent_profile: Any) -> Any:
    """F786 D6 — the POSITION to render for a fleet row.

    The resolved ``position`` column wins (written at assign). For a legacy or
    grandfathered row where it is NULL, fall back to
    :func:`split_effective_name` over ``agent_profile``; when THAT is None (a
    genuine legacy flat name like ``kiro_dev``) render the raw ``agent_profile``.
    This is the one-way legacy reader — the effective name is a pure rendering,
    never re-parsed for behaviour.
    """
    if column_value:
        return column_value
    if not agent_profile:
        return agent_profile
    from cli_agent_orchestrator.utils.agent_profiles import split_effective_name

    split = split_effective_name(agent_profile)
    return split[0] if split is not None else agent_profile


def _current_grok_canonical_hash() -> str | None:
    """F295 AC2: compute sha256 of the current canonical grok config.

    Returns None if the canonical config does not exist or cannot be read.
    """
    try:
        canonical_path = provider_home("grok_cli").home / "config.toml"
        if not canonical_path.exists():
            return None
        text = canonical_path.read_text(encoding="utf-8")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except Exception:
        return None


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


# F789 (#646): the failed-init states that mean the worker is DEAD (or will never
# confirm), mapped to a stable, typed terminal_error CODE for the fleet row. This
# is the durable projection signal — the free-text bridge notice carries the
# richer `code=… reason=…`, but the row itself must say *why* so the TUI never
# renders a dead worker as `working`. Kept intentionally small and derived from
# columns that already exist (no schema migration): the init_state CHECK set.
_INIT_FAILED_TERMINAL_ERROR: dict[str, str] = {
    "init_failed_notified": "init_failed",
    "init_failed_caller_gone": "init_failed_caller_gone",
}


def _terminal_error_code(row: dict[str, Any], init_health: str | None) -> str | None:
    """Derive the projected terminal_error code, or None when the row is healthy.

    F789 (#646): a worker that died at deferred init must surface a typed code on
    its fleet row, and (via the caller) must not project as `working`. Sources,
    in order:
      * an explicit ``init_state`` in the failed set → its mapped code;
      * any other ``init_health == "failed"`` (e.g. an ``init_pending`` row whose
        deadline has passed — the deferred-init TimeoutError window that #646
        reported) → the generic ``deferred_init_failed`` code.
    Returns None for ready/launching/legacy rows.
    """
    init_state = row.get("init_state")
    if isinstance(init_state, str) and init_state in _INIT_FAILED_TERMINAL_ERROR:
        return _INIT_FAILED_TERMINAL_ERROR[init_state]
    if init_health == "failed":
        # Health failed without a recorded terminal init_state — the overdue
        # init_pending window (worker dead, failure not yet claimed). #646's
        # exact signature: still `init_pending`, deadline elapsed.
        return "deferred_init_failed"
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


def _is_config_stale(row: dict[str, Any], canonical_hash: str | None) -> bool | None:
    """F295 AC2: determine if a grok_cli terminal's config is stale.

    Returns True if the terminal's stored config hash differs from the current
    canonical hash.  Returns None for non-grok providers or when comparison is
    not possible (no canonical, no stored hash).
    """
    if row.get("provider") != "grok_cli":
        return None
    if canonical_hash is None:
        return None
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return None
    # D12: read from reserved 'cao' namespace, with legacy top-level fallback (AC13)
    cao_ns = metadata.get("cao")
    if isinstance(cao_ns, dict):
        stored_hash = cao_ns.get("config_sha256")
    else:
        stored_hash = None
    # Legacy fallback: rows stamped by Half 1 before the D12 repoint
    if not isinstance(stored_hash, str):
        stored_hash = metadata.get("config_sha256")
    if not isinstance(stored_hash, str):
        return None
    return stored_hash != canonical_hash


def _is_wedge_suspect(row: dict[str, Any]) -> bool | None:
    """F295 Half 2 AC10: check if a grok_cli terminal is wedge-suspected."""
    if row.get("provider") != "grok_cli":
        return None
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return None
    cao_ns = metadata.get("cao")
    if not isinstance(cao_ns, dict):
        return None
    suspect = cao_ns.get("wedge_suspect")
    if suspect is True:
        return True
    return None


def _child_procs(terminal_id: str) -> list[str] | None:
    """F899 (#751): comms of the live tool subprocesses under this pane, or None.

    PURE — reads only the probe's cache (``peek``), so building a fleet row never
    walks /proc. Returns None when the probe has never run for this terminal (it
    runs on the pane-hold expiry arm alone) or when it could not answer.
    """
    try:
        from cli_agent_orchestrator.services.child_proc_probe import child_proc_probe

        result = child_proc_probe.peek(terminal_id)
    except Exception:
        return None
    if result is None or result.status != "ok" or not result.comms:
        return None
    return list(result.comms)


def _status_since(terminal_id: str) -> str | None:
    """WP-ARCH phase 2, D11 — when a PROJECTED terminal entered its status.

    ``None`` unless the projection is this terminal's publisher of record, which
    is the same predicate the cutover's every other site asks (D1e).  I5 states
    the property as an equality AND a null: it equals
    ``worker_state_shadow.since`` for a projected terminal and is null for an
    unsourced one, and a run where it is non-null for an unsourced terminal fails
    the criterion — that would be a reconstruction wearing the projection's name.

    A read of the projection row, and it is affordable HERE for the reason D1e
    forbids it on ``get_status``: a fleet row is built when an operator or the
    TUI asks, not on the poll path every status consumer rides.  Never raises;
    the fleet renders without it rather than not at all.
    """
    try:
        from cli_agent_orchestrator import bootstrap as _wt_bootstrap
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        if not status_monitor.is_projected(terminal_id):
            return None
        runtime = _wt_bootstrap.current_runtime()
        states = None if runtime is None else runtime.state_store
        if states is None:
            return None
        projection = states.get(terminal_id)
        if projection is None:
            return None
        return projection.since.isoformat()
    except Exception:
        logger.debug("status_since unavailable for %s", terminal_id, exc_info=True)
        return None


def _children_count_from_row(row: dict[str, Any]) -> int:
    """F568 D12a/D12c + F579 D17: length of the children ledger on the fleet row.

    D17 migrated the ledger into the reserved system namespace
    ``metadata_json["cao"]["children"]``; this reader prefers that location and
    falls back to the pre-migration free-form top-level ``children`` key for rows
    written before the migration. The fleet loop already holds this dict
    (``row["metadata"]``), so no extra DB read is taken here. Absent / malformed
    → 0. The sibling ``cao["children_released"]`` ring is never counted.
    """
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return 0
    cao_ns = metadata.get("cao")
    if isinstance(cao_ns, dict) and "children" in cao_ns:
        children = cao_ns.get("children")
        return len(children) if isinstance(children, list) else 0
    children = metadata.get("children")
    return len(children) if isinstance(children, list) else 0


def _observe_model_effort(
    row: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """F826 (#683) D6: resolve + observe live model/effort for one fleet row.

    Binding (D2 / SHOULD-2): the file is bound by the row's recorded identity
    (``id`` for the Claude sidecar; ``provider_session_id`` for codex/pi/kiro),
    NEVER "newest file in a directory". The observation call is wrapped by a
    50 ms deadline and a blanket except so a slow/broken source degrades to the
    configured/unknown fallback rather than blocking the whole fleet build
    (AC5). Returns ``(model_obs, effort_obs)`` additive dicts, or ``(None, None)``
    when the provider is not observable / the source failed.

    The returned dicts carry the configured DB value alongside the observation
    so the TUI can render the ``!`` conflict marker (D1) and the ``[C]`` fallback
    without a second lookup; the configured columns themselves are untouched.
    """
    provider = row.get("provider")
    if not isinstance(provider, str):
        return None, None
    path = _resolve_observation_path(provider, row)
    if path is None:
        return None, None
    try:
        obs = _observe_with_deadline(provider, path, row)
    except Exception:
        logger.debug("f826_observe_failed for %s", row.get("id"), exc_info=True)
        return None, None
    if obs is None:
        return None, None
    return (
        _obs_to_dict(obs.model, row.get("resolved_model")),
        _obs_to_dict(obs.effort, row.get("reasoning_effort")),
    )


def _observe_with_deadline(provider: str, path: Path, row: dict[str, Any]) -> Any:
    """Call the provider adapter under a 50 ms wall-clock deadline (D6/AC5).

    The deadline is advisory: the adapters are all bounded by their own read
    budgets (tail <= 64 KB / capped incremental scan / small checkpoint), so the
    only role of the timer is to record when a source overran for diagnosis --
    the read itself is already bounded, and no subprocess is spawned per GET.
    """
    from cli_agent_orchestrator.services import model_effort_observation as meo

    kwargs: dict[str, Any] = {"generation": row.get("provider_session_id")}
    if provider == "claude_code":
        # An exited/reaped terminal keeps its last value as `[S] exited` (D1).
        kwargs["exited"] = row.get("status") in ("EXITED", "ERROR")
    start = time.monotonic()
    result = meo.observe_provider(provider, path, **kwargs)
    elapsed_ms = (time.monotonic() - start) * 1000.0
    if elapsed_ms > 50.0:
        logger.debug(
            "f826_observe_slow provider=%s terminal=%s elapsed_ms=%.1f",
            provider,
            row.get("id"),
            elapsed_ms,
        )
    return result


def _resolve_observation_path(provider: str, row: dict[str, Any]) -> Path | None:
    """Bind the observation file by the row's recorded identity (D2).

    * ``claude_code`` -- the sidecar ``CAO_HOME_DIR/observe/<terminal_id>.json``,
      bound by terminal id (the emitter writes it keyed by ``CAO_TERMINAL_ID``).
    * ``codex`` -- the rollout under the codex home matched by
      ``provider_session_id`` (exactly one match, else None -- never newest).
    * ``pi_cli`` -- the session JSONL matched by ``provider_session_id``.
    * ``kiro_cli`` -- ``<session_id>.json`` under ``~/.kiro/sessions/cli/``.
    """
    from cli_agent_orchestrator.constants import CAO_HOME_DIR

    terminal_id = row.get("id")
    session_id = row.get("provider_session_id")

    if provider == "claude_code":
        if not isinstance(terminal_id, str) or not terminal_id:
            return None
        return Path(CAO_HOME_DIR) / "observe" / f"{terminal_id}.json"

    if not isinstance(session_id, str) or not session_id:
        return None

    if provider == "codex":
        try:
            home = provider_home("codex").home
        except Exception:
            return None
        matches = list((home / "sessions").glob(f"**/rollout-*{session_id}*.jsonl"))
        return matches[0] if len(matches) == 1 else None

    if provider == "pi_cli":
        base = Path.home() / ".pi" / "agent" / "sessions"
        matches = list(base.glob(f"**/*{session_id}*.jsonl"))
        return matches[0] if len(matches) == 1 else None

    if provider == "kiro_cli":
        candidate = Path.home() / ".kiro" / "sessions" / "cli" / f"{session_id}.json"
        return candidate if candidate.exists() else None

    return None


def _obs_to_dict(obs: Any, configured: Any) -> dict[str, Any]:
    """Render one Observation as the additive wire dict (D6).

    Carries the configured value so the TUI resolves the ``[C]`` fallback and
    the ``!`` conflict marker without a second read. ``event_time`` is the
    integer ns the source stamped (the decay/age clock, S6).
    """
    return {
        "value": obs.value,
        "marker": obs.marker.value,
        "kind": obs.kind.value if obs.kind is not None else None,
        "event_time": obs.event_time_ns,
        "source": obs.source,
        "validity": obs.validity,
        "configured": configured if isinstance(configured, str) else None,
    }


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
    # F716 (#571): delete_terminal opens its F218 teardown intent (DB,
    # committed) BEFORE killing the tmux window and purging the row, so a
    # live row whose window is already gone may be a HEALTHY teardown in
    # flight — not a loss. Load unexpired intent scope keys once per build;
    # the window-absence ERROR override below is suppressed only for those
    # rows (a vanished window with NO teardown intent is still ERROR).
    from cli_agent_orchestrator.services.teardown_intent_service import (
        active_teardown_scope_keys,
    )

    try:
        teardown_scope_keys = active_teardown_scope_keys()
    except Exception:
        logger.exception("f716_teardown_scope_keys_load_failed — fail-closed to ERROR")
        teardown_scope_keys = set()
    by_id = {row["id"]: row for row in rows}
    depths = _depths(rows)
    now = datetime.now(timezone.utc)

    # F295 AC2: compute current canonical hash once per fleet call
    grok_canonical_hash = _current_grok_canonical_hash()

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
        # F506 §8: surface the fusion evidence, rendered by the `cao-fleet` TUI's
        # new columns (F702) — `*` when the fused status differs from what the
        # provider published, and the reason in the row detail. fusion_changed is
        # captured BEFORE the ERROR
        # overrides below so the operator sees "the fusion demoted this", not the
        # quarantine projection.
        fusion_changed = bool(getattr(observation, "fusion_changed", False))
        fusion_reason = getattr(observation, "fusion_reason", None)
        if row.get("recovery_state") not in (None, "rebound"):
            status = TerminalStatus.ERROR
            _wt_legacy_egress.record_fleet_override(  # WP-ARCH F725 #581 hook 2b
                row["id"], "recovery_state", str(row.get("recovery_state"))
            )
        # F716 (#571): window absence under an ACTIVE teardown intent is the
        # healthy delete ordering (intent → window kill → row purge), so keep
        # the observed status instead of stamping ERROR; also expose the
        # teardown state as an additive sibling key (`teardown`, mirroring
        # `delegating`/`fusion_changed`) so the TUI can render `reaping`.
        in_teardown = row["id"] in teardown_scope_keys or (session_name in teardown_scope_keys)
        if has_native_inventory and row["tmux_window"] not in windows and not in_teardown:
            status = TerminalStatus.ERROR
            _wt_legacy_egress.record_fleet_override(  # WP-ARCH F725 #581 hook 2b
                row["id"], "window_absent", str(row["tmux_window"])
            )
        # F124 S1: compute init_health; failed health overrides status to ERROR.
        init_health = _compute_init_health(row, now)
        # F789 (#646): derive the typed terminal_error code for the row. When it
        # is set the worker is dead/never-confirmed at init, so the projected
        # status MUST NOT be `working` (PROCESSING) or any live class — force
        # ERROR. This closes the gap where a deferred-init death (the reported
        # `code=deferred_init_internal` TimeoutError) left the row rendering
        # `● working` with a growing elapsed timer. The code is surfaced on the
        # row below so the TUI can show *why*.
        terminal_error = _terminal_error_code(row, init_health)
        if init_health == "failed" or terminal_error is not None:
            status = TerminalStatus.ERROR
            _wt_legacy_egress.record_fleet_override(  # WP-ARCH F725 #581 hook 2b
                row["id"], "init_health_failed", terminal_error or ""
            )
        # F568 D12c: `delegating` is a projection over the FINAL status (computed
        # here, AFTER all three ERROR overrides above) and the children ledger.
        # An ERROR/quarantined seat never renders `delegating` (r11 S2); a
        # PROCESSING seat keeps `working` (its own turn is open). Only an
        # IDLE/COMPLETED seat with children in flight is `delegating`. The status
        # enum value is left untouched (r11 S3) — `delegating`/`children_count`
        # are additive sibling keys, mirroring `fusion_changed`/`fusion_reason`.
        children_count = _children_count_from_row(row)
        delegating = children_count > 0 and status in (
            TerminalStatus.IDLE,
            TerminalStatus.COMPLETED,
        )
        last_active = _as_utc(row.get("last_active"))
        if last_active is not None:
            since_last_input = max(0.0, (now - last_active).total_seconds())
        else:
            since_last_input = None
        window = windows.get(row["tmux_window"], {})
        # F611 (#467) B2: project the live condition onto the fleet row so
        # /sessions/{name}/fleet carries it (blueprint §3 surface 1). Additive
        # sibling key like fusion_reason/delegating — SEPARATE from `status`
        # (D1), never derived from or feeding fusion. None when no condition.
        # F752 (#609): the fused status goes with the read so a BUSY-class label
        # left over from the last working turn never rides an idle row.
        condition = status_monitor.get_condition(row["id"], status)
        # F826 (#683) D6: observe live model/effort for this terminal. Wrapped —
        # try/except + 50 ms deadline + bounded read + per-terminal isolation —
        # so one bad source never blocks the fleet (AC5). Returns two additive
        # dicts (or None) merged into the row below.
        model_obs, effort_obs = _observe_model_effort(row)
        projected.append(
            {
                "id": row["id"],
                "profile": row.get("agent_profile"),
                "provider": row.get("provider"),
                # F786 (#643) D6: the resolved POSITION for display. Read the
                # column first; for a legacy/grandfathered row where it is NULL,
                # fall back to splitting the effective name, and when that yields
                # None (a genuine legacy flat name) render the raw agent_profile.
                "position": _fleet_position(row.get("position"), row.get("agent_profile")),
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
                # F716 (#571): additive sibling key — True while the row is
                # under an unexpired teardown intent (delete in flight).
                "teardown": in_teardown,
                # F611 (#467): typed provider condition label (CAPPED/BLOCKED/
                # AUTH/…) or None. Rendered by the `cao-fleet` TUI's new columns
                # (F702) in `COND`, and appended to the status cell; distinct
                # from `status`.
                "condition": condition,
                # WP-ARCH phase 2, D11 (I5): when the PROJECTION owns this
                # terminal's status, the moment it entered that state — read
                # from ``worker_state_shadow.since``, which is the only durable
                # record of it.  ``None`` for every unsourced terminal, and that
                # null is the point rather than a gap: the pane path has no such
                # moment to report, and a reconstruction from ``last_active`` or
                # from the fleet's own polling would be a guess wearing the
                # projection's name.  Additive sibling key, like ``condition``
                # and ``fusion_reason``; never derived from or feeding fusion.
                "status_since": _status_since(row["id"]),
                # F506 §8: rendered by the `cao-fleet` TUI's new columns (F702)
                # — `*` is set when fusion_changed is True (the fused status
                # differs from the provider-published one); fusion_reason shows
                # in the row detail.
                "fusion_changed": fusion_changed,
                "fusion_reason": fusion_reason,
                # F899 (#751): the process-tree evidence behind a
                # `child_proc_live` reason — the comms of the live tool
                # subprocesses under this pane, capped at 5. Additive sibling
                # key like fusion_reason; None when the probe has not run for
                # this terminal (it runs only on the pane-hold expiry arm) or
                # when it could not answer. PURE read of the probe's cache —
                # this never triggers a /proc scan of its own.
                "child_procs": _child_procs(row["id"]),
                # F568 D12c: additive sibling keys — the raw `status` enum is
                # unchanged (no new persisted status value). Rendered by the
                # `cao-fleet` TUI's new columns (F702): `DELEG`, and
                # `delegating (N)` in the status cell when `delegating` is True.
                "delegating": delegating,
                "children_count": children_count,
                "init_state": row.get("init_state"),
                "init_health": init_health,
                # F789 (#646): typed reason a worker's row is in ERROR at init
                # (init_failed / init_failed_caller_gone / deferred_init_failed),
                # or None when healthy. Additive sibling key — never a status
                # enum value. Rendered by the fleet TUI so a dead-at-init worker
                # shows the code, not a stale `working`.
                "terminal_error": terminal_error,
                "since_last_input": since_last_input,
                "lifecycle": row.get("lifecycle", "ephemeral"),
                "resolved_model": row.get("resolved_model"),
                # F777 (#634): the effective reasoning effort persisted at spawn,
                # rendered by the `cao-fleet` EFFORT column. None → "-".
                "reasoning_effort": row.get("reasoning_effort"),
                # F826 (#683) D6: additive OBSERVED model/effort, merged from the
                # per-provider observation adapters. Independent per-field objects
                # {value, marker, kind, event_time, source, configured}; the
                # configured DB columns above are NEVER overwritten (D1/Do-NOT).
                # None when the provider is not observable or the source failed —
                # the TUI then renders the configured `[C]` / `[?]` fallback.
                "model_obs": model_obs,
                "effort_obs": effort_obs,
                "reparented_from": row.get("reparented_from"),
                # F295 AC2: config_stale for grok_cli terminals
                "config_stale": _is_config_stale(row, grok_canonical_hash),
                # F295 Half 2 AC10: wedge_suspect for grok_cli terminals
                "wedge_suspect": _is_wedge_suspect(row),
            }
        )
    # F476 B5-r2: include wake-exhaustion alarms in fleet projection
    wake_alarms = get_wake_exhaustion_alarms()
    return {
        "session_name": session_name,
        "terminals": projected,
        "wake_exhaustion_alarms": wake_alarms,
    }


def get_wake_exhaustion_alarms() -> list[dict[str, Any]]:
    """F476 B5: Return active wake-exhaustion alarms for fleet/dashboard surfaces."""
    from cli_agent_orchestrator.clients.database import (
        _WAKE_STREAK_CAP,
        MailboxModel,
        SessionLocal,
    )

    alarms: list[dict[str, Any]] = []
    with SessionLocal() as db:
        exhausted = (
            db.query(MailboxModel)
            .filter(
                MailboxModel.wake_streak >= _WAKE_STREAK_CAP,
                MailboxModel.wake_notified_id > MailboxModel.consumed_through_id,
            )
            .all()
        )
        for mb in exhausted:
            alarms.append(
                {
                    "mailbox_id": mb.id,
                    "session_name": mb.session_name,
                    "role": mb.role,
                    "stuck_row_id": int(mb.wake_notified_id),
                    "wake_streak": int(mb.wake_streak),
                }
            )
    return alarms
