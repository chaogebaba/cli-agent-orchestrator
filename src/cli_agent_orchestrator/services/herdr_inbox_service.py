"""HerdrInboxService — socket event-based inbox delivery for herdr backend.

Replaces the pipe-pane + file watchdog approach with herdr's native socket API.
Subscribes to a broadcast pane.updated event (whose payload carries
agent_status) and delivers pending inbox messages when a pane transitions to
idle or done.

Design:
- Maintains a pane_id → terminal_id map for managed panes
- Subscribes once to a broadcast pane.updated (no pane_id) covering all panes,
  so a newly registered pane's events already arrive — registration updates the
  map only and never re-subscribes or forces a reconnect
- Reconnects with exponential backoff on socket disconnect
- Supplements with periodic pane read for kiro-cli (working >30s check)
"""

import asyncio
import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal, Optional, Set

from cli_agent_orchestrator.models.native_publish import NativePublishRequest

logger = logging.getLogger(__name__)

# Exponential backoff parameters
_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_MAX = 30.0  # seconds
_BACKOFF_MULTIPLIER = 2.0

# Kiro supplement check: how long in "working" before we check pane read
_KIRO_WORKING_THRESHOLD = 30.0  # seconds
INTENT_ACK_WAIT_S = 5.0
TEARDOWN_INTENT_TTL_S = 60.0
RECONNECT_GRACE_S = 30.0
SELF_TEST_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class IdentityMarker:
    agent: str
    pane_id: str
    native_event_gen: int


@dataclass(frozen=True)
class ReconcileOutcome:
    status: Literal["ok", "failed"]
    confirmed: frozenset[tuple[str, str, int]] = frozenset()


@dataclass
class _IdentityRecord:
    marker: IdentityMarker
    received_monotonic: float
    quarantined: bool = False
    grace_started: float | None = None


class HerdrInboxService:
    """Event-driven inbox delivery service using herdr socket API.

    Subscribes to agent status events for managed panes and delivers
    pending messages when agents become idle/done.
    """

    def __init__(
        self,
        socket_path: Optional[str] = None,
        delivery_callback: Optional[Callable[[str], None]] = None,
        native_publish_callback: Callable[[NativePublishRequest], None] | None = None,
        native_activation_callback: Callable[[], None] | None = None,
        herdr_session: str = "cao",
    ) -> None:
        """Initialize the inbox service.

        Args:
            socket_path: Path to herdr socket. None = auto-detect from env.
            delivery_callback: Function to call for message delivery.
                Signature: callback(terminal_id) → checks and delivers pending messages.
            herdr_session: Name of the herdr session to connect to. Used to
                derive the default socket path and prefix CLI calls.
        """
        self._herdr_session = herdr_session
        self._socket_path = socket_path or self._default_socket_path(herdr_session)
        self._delivery_callback = delivery_callback
        self._native_publish_callback = native_publish_callback
        self._native_activation_callback = native_activation_callback
        self._publisher_enabled = False
        self._publisher_self_test_attempted = False

        # Managed pane tracking
        self._pane_to_terminal: Dict[str, str] = {}  # pane_id → terminal_id
        self._terminal_to_pane: Dict[str, str] = {}  # terminal_id → pane_id
        self._native_event_gen: Dict[tuple[str, str], int] = {}
        self._identity_guard = threading.Lock()
        self._identity_records: Dict[tuple[str, str, int], _IdentityRecord] = {}

        # Kiro-specific tracking for supplement check
        self._kiro_terminals: Set[str] = set()  # terminal_ids using kiro-cli
        self._working_since: Dict[str, float] = {}  # terminal_id → timestamp

        # Workspace tracking for lifecycle events
        self._workspace_to_session: Dict[str, str] = {}  # workspace_id → session_name
        self._lifecycle_tasks: Set[asyncio.Task] = set()
        self._workspace_close_routes: Dict[str, asyncio.Task] = {}

        # Connection state
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._backoff = _BACKOFF_BASE

    @staticmethod
    def _default_socket_path(session_name: str = "cao") -> str:
        """Determine default herdr socket path for a named session.

        The default session (name ``"default"``) uses a flat path:
        ``~/.config/herdr/herdr.sock``.

        Named sessions use a sessions subdirectory:
        ``~/.config/herdr/sessions/<session_name>/herdr.sock``.

        Args:
            session_name: Herdr session name. Defaults to ``"cao"``.
        """
        import os
        from pathlib import Path

        # Check XDG_CONFIG_HOME first, fallback to ~/.config
        config_home = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        if session_name == "default":
            return f"{config_home}/herdr/herdr.sock"
        return f"{config_home}/herdr/sessions/{session_name}/herdr.sock"

    def _invalidate_terminal_identity_locked(self, terminal_id: str) -> None:
        for key in [key for key in self._identity_records if key[0] == terminal_id]:
            self._identity_records.pop(key, None)

    def _register_terminal_locked(self, terminal_id: str, pane_id: str, is_kiro: bool) -> None:
        prior_pane = self._terminal_to_pane.get(terminal_id)
        self._invalidate_terminal_identity_locked(terminal_id)
        if prior_pane and prior_pane != pane_id:
            self._pane_to_terminal.pop(prior_pane, None)
        self._pane_to_terminal[pane_id] = terminal_id
        self._terminal_to_pane[terminal_id] = pane_id
        if is_kiro:
            self._kiro_terminals.add(terminal_id)

    def register_terminal(self, terminal_id: str, pane_id: str, is_kiro: bool = False) -> None:
        """Register a terminal for event-based inbox delivery.

        Args:
            terminal_id: CAO terminal identifier
            pane_id: Current herdr compact pane_id
            is_kiro: Whether this terminal runs kiro-cli (enables supplement check)
        """
        from cli_agent_orchestrator.services.inbox_service import get_delivery_lock

        with get_delivery_lock(terminal_id):
            with self._identity_guard:
                self._register_terminal_locked(terminal_id, pane_id, is_kiro)

        logger.info(f"Registered terminal {terminal_id} (pane={pane_id}, kiro={is_kiro})")

    def _register_terminal_under_guard(
        self,
        terminal_id: str,
        pane_id: str,
        is_kiro: bool,
        guard: Any,
    ) -> None:
        """Register while a same-terminal active DeliveryGuard owns the lock."""
        if getattr(guard, "terminal_id", None) != terminal_id or guard.active is not True:
            raise RuntimeError("delivery_guard_not_active_for_terminal")
        with self._identity_guard:
            self._register_terminal_locked(terminal_id, pane_id, is_kiro)

    def unregister_terminal(self, terminal_id: str) -> None:
        """Remove a terminal from managed set.

        Args:
            terminal_id: Terminal to unregister
        """
        from cli_agent_orchestrator.services.inbox_service import get_delivery_lock

        with get_delivery_lock(terminal_id):
            with self._identity_guard:
                pane_id = self._terminal_to_pane.pop(terminal_id, None)
                if pane_id:
                    self._pane_to_terminal.pop(pane_id, None)
                self._kiro_terminals.discard(terminal_id)
                self._working_since.pop(terminal_id, None)
                self._invalidate_terminal_identity_locked(terminal_id)
        logger.info(f"Unregistered terminal {terminal_id}")

    def read_identity_marker(self, terminal_id: str) -> IdentityMarker | None:
        """Copy the authoritative marker for the terminal's current pane incarnation."""
        now = time.monotonic()
        with self._identity_guard:
            pane_id = self._terminal_to_pane.get(terminal_id)
            if pane_id is None:
                return None
            generation = self._native_event_gen.get((terminal_id, pane_id), 0)
            record = self._identity_records.get((terminal_id, pane_id, generation))
            if record is None or record.quarantined:
                return None
            if record.grace_started is not None and now - record.grace_started < RECONNECT_GRACE_S:
                return None
            return record.marker

    def _quarantine_identity_markers(self) -> None:
        with self._identity_guard:
            for record in self._identity_records.values():
                record.quarantined = True

    def _apply_reconcile_outcome(self, outcome: ReconcileOutcome) -> None:
        now = time.monotonic()
        with self._identity_guard:
            if outcome.status == "failed":
                for record in self._identity_records.values():
                    record.quarantined = True
                return
            for key in list(self._identity_records):
                record = self._identity_records[key]
                if key not in outcome.confirmed:
                    self._identity_records.pop(key, None)
                    continue
                record.quarantined = False
                record.grace_started = now

    def _confirmed_incarnations(self) -> frozenset[tuple[str, str, int]]:
        with self._identity_guard:
            return frozenset(
                (
                    terminal_id,
                    pane_id,
                    self._native_event_gen.get((terminal_id, pane_id), 0),
                )
                for terminal_id, pane_id in self._terminal_to_pane.items()
            )

    def _remap_terminal_identity(
        self, terminal_id: str, old_pane_id: str, new_pane_id: str
    ) -> None:
        from cli_agent_orchestrator.services.inbox_service import get_delivery_lock

        with get_delivery_lock(terminal_id):
            with self._identity_guard:
                if self._terminal_to_pane.get(terminal_id) != old_pane_id:
                    return
                self._pane_to_terminal.pop(old_pane_id, None)
                self._pane_to_terminal[new_pane_id] = terminal_id
                self._terminal_to_pane[terminal_id] = new_pane_id
                self._invalidate_terminal_identity_locked(terminal_id)

    def _drop_terminal_identity(self, terminal_id: str, pane_id: str) -> None:
        from cli_agent_orchestrator.services.inbox_service import get_delivery_lock

        with get_delivery_lock(terminal_id):
            with self._identity_guard:
                self._pane_to_terminal.pop(pane_id, None)
                self._terminal_to_pane.pop(terminal_id, None)
                self._kiro_terminals.discard(terminal_id)
                self._working_since.pop(terminal_id, None)
                self._invalidate_terminal_identity_locked(terminal_id)

    async def start(self) -> None:
        """Start the event loop: wait for first terminal, then connect and listen."""
        # Run DB cleanup before starting the socket loop so ghost records from
        # prior server runs are removed even when no terminals are registered yet.
        await self._startup_db_cleanup()
        kiro_task = asyncio.ensure_future(self._kiro_supplement_loop())
        try:
            await self._socket_loop()
        finally:
            kiro_task.cancel()
            tasks = list(self._lifecycle_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _startup_db_cleanup(self) -> None:
        """Delete ghost DB terminals whose herdr tabs no longer exist.

        Runs once at server startup before any pane registrations.  Cannot
        rely on _pane_to_terminal (empty at startup) or _workspace_to_session
        (populated later by _reconcile).  Builds the workspace map directly
        from a herdr api snapshot.
        """
        from cli_agent_orchestrator.clients.database import list_terminals_by_session
        from cli_agent_orchestrator.services import terminal_service

        snapshot = self._fetch_snapshot()
        if snapshot is None:
            logger.debug("Startup DB cleanup: no snapshot, skipping")
            return

        # workspace_id -> label (= CAO session name). Skip malformed records.
        workspace_to_session = {
            ws["workspace_id"]: ws["label"]
            for ws in snapshot.get("workspaces", [])
            if ws.get("workspace_id") and ws.get("label")
        }

        # workspace_id -> set of live tab labels (= CAO window names)
        live_tabs_by_workspace: Dict[str, set] = {}
        for tab in snapshot.get("tabs", []):
            ws_id = tab.get("workspace_id", "")
            label = tab.get("label", "")
            if ws_id and label:
                live_tabs_by_workspace.setdefault(ws_id, set()).add(label)

        deleted = 0
        for ws_id, session_name in workspace_to_session.items():
            live_labels = live_tabs_by_workspace.get(ws_id, set())
            db_terminals = list_terminals_by_session(session_name)
            for term in db_terminals:
                window = term.get("tmux_window", "")
                if window and window not in live_labels:
                    if term.get("init_state") != "ready":
                        logger.warning(
                            "herdr_startup_cleanup_skipped_non_ready terminal=%s init_state=%r",
                            term["id"],
                            term.get("init_state"),
                        )
                        continue
                    logger.info(
                        f"Startup DB cleanup: deleting ghost terminal {term['id']} "
                        f"({session_name}:{window}) — tab not in herdr"
                    )
                    try:
                        terminal_service._delete_terminal_core(term["id"])
                        deleted += 1
                    except Exception as e:
                        logger.warning(
                            f"Startup DB cleanup: failed to delete ghost terminal "
                            f"{term['id']}: {e}"
                        )

        if deleted:
            logger.info(f"Startup DB cleanup: removed {deleted} ghost terminal(s)")
        else:
            logger.debug("Startup DB cleanup: no ghost terminals found")

    async def _kiro_supplement_loop(self) -> None:
        """Periodically check kiro terminals stuck in working state."""
        while True:
            await asyncio.sleep(10.0)
            try:
                await self.check_kiro_supplements()
            except Exception:
                logger.debug("Kiro supplement check error", exc_info=True)

    async def _socket_loop(self) -> None:
        """Connect to herdr socket and listen for events with reconnect.

        Defers connection until at least one terminal is registered. This avoids
        the disconnect/reconnect churn caused by herdr closing idle connections
        that have no active subscriptions.
        """
        while True:
            # Wait until there is at least one pane to subscribe to
            while not self._pane_to_terminal:
                await asyncio.sleep(0.5)

            try:
                await self._connect()

                # Reconcile map against live herdr state before subscribing
                reconcile = await self._reconcile()
                self._apply_reconcile_outcome(reconcile)
                if reconcile.status == "failed":
                    raise ConnectionError("Herdr reconciliation failed")

                # Subscribe to everything in ONE events.subscribe call: a single
                # broadcast pane.updated (no pane_id) plus the lifecycle events.
                # herdr resets the connection on a second events.subscribe, so
                # this must be a single combined call.
                await self._subscribe_all_events()

                if (
                    not self._publisher_self_test_attempted
                    and self._native_publish_callback is not None
                ):
                    self._publisher_self_test_attempted = True
                    if await self._native_publisher_self_test():
                        self._publisher_enabled = True
                        if self._native_activation_callback is not None:
                            self._native_activation_callback()
                    else:
                        logger.warning(
                            "native publisher startup self-test failed; legacy authority retained"
                        )

                self._backoff = _BACKOFF_BASE  # Reset backoff after successful setup

                # Listen for events
                await self._event_loop()

            except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
                logger.warning(f"Herdr socket disconnected: {e}")
                self._quarantine_identity_markers()

                # Exponential backoff
                logger.info(f"Reconnecting in {self._backoff}s...")
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * _BACKOFF_MULTIPLIER, _BACKOFF_MAX)

    def _fetch_snapshot(self) -> Optional[dict]:
        """Return herdr's full live session snapshot in one socket call.

        `herdr api snapshot` returns result.snapshot with panes[]/tabs[]/
        workspaces[]. Each pane carries pane_id, terminal_id, agent_status,
        tab_id, workspace_id; each tab carries tab_id, label, workspace_id;
        each workspace carries workspace_id, label. Replaces the former
        pane-list + workspace-list + tab-list subprocess fan-out.

        Returns None on any failure (non-zero exit, timeout, missing binary,
        or malformed output). This is the single entry point all snapshot reads
        route through, so it swallows the same broad error set as the file's
        other herdr-subprocess helpers rather than letting one bad response
        kill the socket loop.
        """
        try:
            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "api", "snapshot"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                # repr() the stderr: it can echo user-controlled labels/args and
                # may contain newlines/control chars that would otherwise forge
                # log lines. Matches the escaping used elsewhere in the codebase.
                logger.warning("Snapshot: `api snapshot` failed: %r", result.stderr)
                return None
            snapshot = json.loads(result.stdout)["result"]["snapshot"]
            if not isinstance(snapshot, dict):
                logger.warning("Snapshot: result.snapshot is not a dict; ignoring")
                return None
            return snapshot
        except (
            subprocess.SubprocessError,
            OSError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as e:
            logger.warning(f"Snapshot: failed to fetch/parse: {e}")
            return None

    async def _reconcile(self) -> ReconcileOutcome:
        """Reconcile _pane_to_terminal map against live herdr state.

        Prunes stale pane entries, deletes orphaned DB terminal records,
        and kills workspaces with zero live terminals.
        """
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import (
            get_terminal_metadata,
            list_terminals_by_session,
            record_workspace_mapping,
        )

        # One socket call replaces the former pane-list + workspace-list +
        # tab-list subprocess fan-out. All three data structures below are derived
        # from this single snapshot.
        snapshot = self._fetch_snapshot()
        if snapshot is None:
            logger.warning("Reconcile: no snapshot")
            return ReconcileOutcome("failed")

        # Live pane_ids (from snapshot.panes).
        live_pane_ids = {p["pane_id"] for p in snapshot.get("panes", []) if p.get("pane_id")}

        # workspace_id -> label (= CAO session name), from snapshot.workspaces.
        # Skip malformed records (missing id/label) rather than letting a
        # KeyError escape _reconcile and kill the socket loop — matches the
        # defensive .get() style used in the tabs loop below.
        workspace_to_session = {
            ws["workspace_id"]: ws["label"]
            for ws in snapshot.get("workspaces", [])
            if ws.get("workspace_id") and ws.get("label")
        }
        with self._identity_guard:
            self._workspace_to_session = workspace_to_session
        for ws_id, session_name in workspace_to_session.items():
            try:
                record_workspace_mapping(ws_id, session_name)
            except Exception:
                logger.exception(
                    "herdr_workspace_map_backfill_failed workspace=%s session=%s",
                    ws_id,
                    session_name,
                )

        # workspace_id -> set of live tab labels (= CAO window names), from
        # snapshot.tabs.
        live_tabs_by_workspace: Dict[str, set] = {}
        for tab in snapshot.get("tabs", []):
            ws_id = tab.get("workspace_id", "")
            label = tab.get("label", "")
            if ws_id and label:
                live_tabs_by_workspace.setdefault(ws_id, set()).add(label)

        # DB cross-check: find terminals in DB whose tab no longer exists in herdr.
        # This catches ghost records from previous server runs where _pane_to_terminal
        # starts empty (so the stale-pane diff below produces nothing).
        for ws_id, session_name in self._workspace_to_session.items():
            live_labels = live_tabs_by_workspace.get(ws_id, set())
            db_terminals = list_terminals_by_session(session_name)
            for term in db_terminals:
                window = term.get("tmux_window", "")
                if window and window not in live_labels:
                    logger.info(
                        f"Reconcile: deleting ghost terminal {term['id']} "
                        f"({session_name}:{window}) — tab not in herdr"
                    )
                    try:
                        await self._route_spontaneous_terminal(term)
                    except Exception as e:
                        logger.warning(
                            f"Reconcile: failed to delete ghost terminal {term['id']}: {e}"
                        )

        # Find stale panes: stored pane_id no longer in herdr's live pane list.
        #
        # A stale pane_id does NOT mean the terminal is dead. herdr renumbers
        # compact pane_ids when a sibling tab in the workspace closes, so a
        # still-running terminal's stored pane_id can fall out of the live list
        # while its tab is very much alive. Identity must come from the durable
        # tab label (tmux_window), never the ephemeral pane_id.
        stale_pane_ids = set(self._pane_to_terminal.keys()) - live_pane_ids
        if not stale_pane_ids:
            logger.debug("Reconcile: all panes live, nothing to prune")
            return ReconcileOutcome("ok", self._confirmed_incarnations())

        # Live workspace labels, used to gate workspace teardown below: never
        # kill a workspace whose label is still present in herdr.
        live_workspace_labels = set(self._workspace_to_session.values())

        # Sessions that genuinely lost a terminal (deleted, not re-mapped).
        affected_sessions: Set[str] = set()
        remapped = 0
        deleted = 0

        for pane_id in stale_pane_ids:
            terminal_id = self._pane_to_terminal.get(pane_id)
            if not terminal_id:
                self._pane_to_terminal.pop(pane_id, None)
                continue

            # Session/window identity before any mutation.
            meta = get_terminal_metadata(terminal_id)
            term_session: Optional[str] = meta["tmux_session"] if meta else None
            term_window: Optional[str] = meta["tmux_window"] if meta else None

            # Re-map renumbered-but-live panes instead of deleting. A live tab
            # label means the pane_id was renumbered, not closed: re-resolve the
            # current pane_id and update both maps. Only when re-resolution fails
            # do we fall through to the delete path.
            if term_window and self._label_still_live(term_window):
                try:
                    # Invalidate pane cache so get_pane_id does a fresh label-based
                    # lookup instead of returning the stale pane_id we just proved
                    # is no longer live. See PR #309 review comment.
                    backend = get_backend()
                    if hasattr(backend, "_pane_cache"):
                        backend._pane_cache.pop(terminal_id, None)
                    new_pane_id = backend.get_pane_id(terminal_id, term_session or "", term_window)
                except Exception as e:
                    logger.warning(
                        "Reconcile: tab %s live but pane re-resolve failed for %s (%s); "
                        "deleting",
                        term_window,
                        terminal_id,
                        e,
                    )
                else:
                    await asyncio.to_thread(
                        self._remap_terminal_identity,
                        terminal_id,
                        pane_id,
                        new_pane_id,
                    )
                    logger.info(
                        "Reconcile: re-mapped %s %s -> %s (pane renumbered, tab still live)",
                        terminal_id,
                        pane_id,
                        new_pane_id,
                    )
                    remapped += 1
                    continue

            # Tab label genuinely gone (or re-resolve failed): prune maps and
            # delete the orphaned DB record.
            await asyncio.to_thread(self._drop_terminal_identity, terminal_id, pane_id)

            try:
                if meta and await self._route_spontaneous_terminal(meta):
                    deleted += 1
            except Exception as e:
                logger.warning(f"Reconcile: failed to delete terminal {terminal_id}: {e}")

            if term_session:
                affected_sessions.add(term_session)

        # Kill a workspace only when its label is gone from herdr AND no managed
        # terminal remains for the session. A live label means the workspace is
        # alive and its panes were merely renumbered — killing it would tear down
        # working agents.
        if affected_sessions:
            remaining_by_session: Dict[str, int] = {s: 0 for s in affected_sessions}
            for tid in self._terminal_to_pane:
                meta = get_terminal_metadata(tid)
                if meta and meta["tmux_session"] in remaining_by_session:
                    remaining_by_session[meta["tmux_session"]] += 1

            for session_name, remaining in remaining_by_session.items():
                if remaining == 0 and session_name not in live_workspace_labels:
                    try:
                        get_backend().kill_session(session_name)
                        logger.info(f"Reconcile: killed empty workspace {session_name}")
                    except Exception as e:
                        logger.warning(f"Reconcile: failed to kill workspace {session_name}: {e}")

        logger.info(
            "Reconcile: %d stale pane(s) — %d re-mapped, %d deleted",
            len(stale_pane_ids),
            remapped,
            deleted,
        )
        return ReconcileOutcome("ok", self._confirmed_incarnations())

    async def _connect(self) -> None:
        """Connect to the herdr socket."""
        self._reader, self._writer = await asyncio.open_unix_connection(self._socket_path)
        logger.info(f"Connected to herdr socket: {self._socket_path}")

    async def _native_publisher_self_test(self) -> bool:
        """Prove the subscribed server is queryable without consuming socket data."""

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    subprocess.run,
                    ["herdr", "--session", self._herdr_session, "pane", "list"],
                    capture_output=True,
                    text=True,
                    timeout=SELF_TEST_TIMEOUT_S,
                ),
                timeout=SELF_TEST_TIMEOUT_S,
            )
            return result.returncode == 0
        except (asyncio.TimeoutError, subprocess.SubprocessError, OSError):
            return False

    async def _subscribe_all_events(self) -> None:
        """Subscribe to all events in a SINGLE events.subscribe call.

        herdr (0.7.5) resets the entire connection when it receives a second
        events.subscribe on a connection that already has an active
        subscription, so this must remain exactly one events.subscribe per
        connection.

        The subscription is a broadcast pane.updated (sent with NO pane_id):
        herdr streams it for every pane and its payload carries agent_status,
        so a single broadcast subscription replaces the former per-pane
        pane.agent_status_changed subscriptions. This is independent of
        _pane_to_terminal — no per-pane enumeration is needed. The pane.closed
        and workspace.closed lifecycle events are sent in the same call.
        """
        subscriptions = [
            {"type": "pane.updated"},
            {"type": "pane.closed"},
            {"type": "workspace.closed"},
        ]
        message = {
            "id": "sub_all",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
        await self._send(message)
        logger.info(
            "Subscribed to broadcast pane.updated + lifecycle events "
            "in one events.subscribe call"
        )

    async def _event_loop(self) -> None:
        """Listen for events and dispatch delivery."""
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                raise ConnectionError("Socket closed")

            try:
                event = json.loads(line.decode())
            except json.JSONDecodeError:
                continue

            # herdr identifies the event in the "event" key. Lifecycle events use
            # underscore names (pane_closed / workspace_closed); the agent-status
            # event uses the dotted name (pane.agent_status_changed). Normalize the
            # name so routing does not depend on the separator herdr happens to use.
            # (Older code read "type" and matched dotted lifecycle names, which never
            # matched herdr's real wire format — lifecycle cleanup silently never ran.)
            raw_event = event.get("event", "") or event.get("type", "")
            event_name = raw_event.replace("_", ".")

            # Handle lifecycle events
            if event_name in ("pane.closed", "workspace.closed"):
                self._handle_lifecycle_event(event_name, event.get("data", {}))
                continue

            data = event.get("data", {})
            # Broadcast pane.updated nests the pane under data.pane; retired
            # agent-status events used top-level data. Fall back to data, and
            # guard against a null/non-dict pane so one malformed event cannot
            # escape the loop and kill delivery.
            pane_obj = data.get("pane") or data
            if not isinstance(pane_obj, dict):
                pane_obj = {}
            pane_id = pane_obj.get("pane_id", "")
            status = pane_obj.get("agent_status", "")

            # Only process events for managed panes
            with self._identity_guard:
                terminal_id = self._pane_to_terminal.get(pane_id)
                if not terminal_id:
                    continue
                key = (terminal_id, pane_id)
                generation = self._native_event_gen.get(key, 0) + 1
                self._native_event_gen[key] = generation
                agent = pane_obj.get("agent") or data.get("agent")
                if isinstance(agent, str) and agent:
                    self._invalidate_terminal_identity_locked(terminal_id)
                    marker = IdentityMarker(agent, pane_id, generation)
                    self._identity_records[(terminal_id, pane_id, generation)] = _IdentityRecord(
                        marker, time.monotonic()
                    )

            if (
                self._publisher_enabled
                and (
                    event_name == "pane.updated"
                    or raw_event in {"pane_agent_status_changed", "pane.agent_status_changed"}
                )
                and isinstance(status, str)
                and status
                and self._native_publish_callback is not None
            ):
                try:
                    self._native_publish_callback(
                        NativePublishRequest(
                            terminal_id,
                            pane_id,
                            generation,
                            status,
                            time.monotonic(),
                        )
                    )
                except Exception:
                    logger.exception("native status publication failed for %s", terminal_id)

            if status in ("idle", "done"):
                # Clear working timestamp
                self._working_since.pop(terminal_id, None)
                # Trigger delivery
                self._deliver(terminal_id)

            elif status == "working":
                # Track working start for kiro supplement check
                if terminal_id in self._kiro_terminals:
                    if terminal_id not in self._working_since:
                        self._working_since[terminal_id] = time.time()

    def get_native_event_gen(self, terminal_id: str, pane_id: str) -> int:
        """Return native status events routed from this exact pane incarnation."""
        with self._identity_guard:
            return self._native_event_gen.get((terminal_id, pane_id), 0)

    def _label_still_live(self, window_name: str) -> bool:
        """Return True if a tab with this label is still live in herdr.

        Used to disambiguate herdr's reused compact pane_ids on replayed
        pane_closed events. The tab label is unique per incarnation, so a live
        label means the close event refers to an older incarnation and is stale.

        Fails toward False (not live) when herdr can't be queried, so the caller
        proceeds with cleanup rather than leaving a possibly-closed terminal.
        """
        try:
            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "tab", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    "_label_still_live: herdr tab list failed (rc=%s): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return False
            tab_data = json.loads(result.stdout)
            tabs = tab_data.get("result", {}).get("tabs", [])
            live_labels = {tab.get("label", "") for tab in tabs}
            return window_name in live_labels
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("_label_still_live: could not query herdr (%s)", e)
            return False

    def _handle_lifecycle_event(self, event_type: str, data: dict) -> None:
        """Schedule lifecycle cleanup without blocking the socket readline task."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._handle_lifecycle_event_async(event_type, dict(data)))
            return
        if event_type == "workspace.closed":
            workspace_id = data.get("workspace_id", "")
            if not workspace_id or workspace_id in self._workspace_close_routes:
                return
            task = loop.create_task(self._handle_workspace_close_owned(dict(data)))
            self._workspace_close_routes[workspace_id] = task
        else:
            task = loop.create_task(self._handle_lifecycle_event_async(event_type, dict(data)))
        self._lifecycle_tasks.add(task)
        task.add_done_callback(self._lifecycle_tasks.discard)

    async def _handle_workspace_close_owned(self, data: dict) -> None:
        workspace_id = data.get("workspace_id", "")
        try:
            await self._handle_lifecycle_event_async("workspace.closed", data)
        finally:
            current = asyncio.current_task()
            if self._workspace_close_routes.get(workspace_id) is current:
                self._workspace_close_routes.pop(workspace_id, None)

    async def _route_spontaneous_terminal(self, metadata: dict) -> bool:
        """Apply class 1/2/settlement routing to one vanished terminal."""
        from cli_agent_orchestrator.services import terminal_service

        terminal_id = metadata["id"]
        state = metadata.get("init_state")
        if state == "init_pending":
            try:
                await terminal_service.quiesce_deferred_terminal(terminal_id)
            except Exception as exc:
                logger.error("herdr_class2_quiesce_failed terminal=%s code=%s", terminal_id, exc)
                return False
            await terminal_service._claim_and_settle_deferred_failure(
                terminal_id,
                f"herdr-{time.monotonic_ns()}",
                metadata,
                "worker_vanished",
                None,
            )
            return True
        if state == "ready":
            terminal_service._delete_terminal_core(terminal_id)
            return True
        if state in {"init_failed_notified", "init_failed_caller_gone"}:
            await terminal_service.dispatcher.run(
                terminal_id,
                "herdr-settle",
                "mutating",
                "settlement",
                terminal_service._settle_deferred_failure_sync,
                terminal_id,
            )
            return True
        logger.error(
            "herdr_cleanup_skipped_invalid_init_state terminal=%s init_state=%r",
            terminal_id,
            state,
        )
        return False

    async def _resolve_issuing_intent(self, workspace_id: str) -> None:
        from cli_agent_orchestrator.clients.database import get_teardown_intent

        deadline = time.monotonic() + INTENT_ACK_WAIT_S
        while time.monotonic() < deadline:
            intent = get_teardown_intent(workspace_id)
            if intent is None or intent.get("state") == "void":
                await self._route_workspace_close(workspace_id, proven=False)
                return
            if intent.get("state") == "issued_ok":
                await self._route_workspace_close(workspace_id, proven=True)
                return
            await asyncio.sleep(min(0.05, deadline - time.monotonic()))
        await self._route_workspace_close(workspace_id, proven=False)

    async def _route_workspace_close(self, workspace_id: str, *, proven: bool) -> None:
        from cli_agent_orchestrator.clients.database import (
            consume_current_teardown_intent,
            current_workspace_for_session,
            get_teardown_intent,
            list_terminals_by_session,
            resolve_workspace_mapping,
            retire_workspace_mapping,
        )
        from cli_agent_orchestrator.services import terminal_service

        intent = None
        if proven:
            intent = consume_current_teardown_intent(
                workspace_id,
                ttl_s=TEARDOWN_INTENT_TTL_S,
            )
            if intent is None:
                proven = False
        session_name = (
            intent.get("session_name") if intent else resolve_workspace_mapping(workspace_id)
        )
        if not session_name:
            logger.error("workspace_close_unroutable workspace=%s", workspace_id)
            return
        if current_workspace_for_session(session_name) != workspace_id:
            logger.warning(
                "workspace_close_stale_generation workspace=%s session=%s",
                workspace_id,
                session_name,
            )
            retire_workspace_mapping(workspace_id)
            return
        terminals = list_terminals_by_session(session_name)
        completed = False
        try:
            if proven:
                await terminal_service.quiesce_deferred_terminals(terminals)
                for terminal in terminals:
                    terminal_service._delete_terminal_core(terminal["id"])
            else:
                for terminal in terminals:
                    if not await self._route_spontaneous_terminal(terminal):
                        return
            completed = True
        finally:
            if completed:
                retire_workspace_mapping(workspace_id)
                with self._identity_guard:
                    self._workspace_to_session.pop(workspace_id, None)

    async def _handle_lifecycle_event_async(self, event_type: str, data: dict) -> None:
        from cli_agent_orchestrator.clients.database import (
            get_teardown_intent,
            get_terminal_metadata,
        )

        if event_type == "pane.closed":
            pane_id = data.get("pane_id", "")
            with self._identity_guard:
                terminal_id = self._pane_to_terminal.get(pane_id)
            if not terminal_id:
                return
            metadata = get_terminal_metadata(terminal_id)
            if metadata is None:
                return
            window_name = metadata.get("tmux_window")
            # Compact pane IDs can be reused and lifecycle events replayed after
            # reconnect. The durable tab label distinguishes a stale close from
            # the current terminal incarnation.
            if window_name and self._label_still_live(window_name):
                logger.info(
                    "pane.closed: ignoring stale close for %s (pane=%s)",
                    terminal_id,
                    pane_id,
                )
                return
            if not await self._route_spontaneous_terminal(metadata):
                return
            await asyncio.to_thread(self._drop_terminal_identity, terminal_id, pane_id)
            return

        if event_type != "workspace.closed":
            return
        workspace_id = data.get("workspace_id", "")
        if not workspace_id:
            return
        try:
            intent = get_teardown_intent(workspace_id)
        except Exception:
            logger.exception("workspace_close_intent_store_unavailable workspace=%s", workspace_id)
            intent = None
        if intent and intent.get("state") == "issuing":
            await self._resolve_issuing_intent(workspace_id)
            return
        await self._route_workspace_close(
            workspace_id, proven=bool(intent and intent.get("state") == "issued_ok")
        )

    # TODO: _deliver() calls callback synchronously — if callback is async,
    # this will need a threadsafe bridge (out of scope for this change).
    def _deliver(self, terminal_id: str) -> None:
        """Check and deliver pending messages for a terminal."""
        if self._delivery_callback:
            try:
                self._delivery_callback(terminal_id)
            except Exception as e:
                logger.error(f"Delivery failed for terminal {terminal_id}: {e}")

    async def check_kiro_supplements(self) -> None:
        """Periodic check for kiro-cli terminals stuck in 'working' state.

        For terminals in 'working' for >30s, read pane content and check
        for permission prompt patterns.
        """
        import subprocess

        now = time.time()
        for terminal_id in list(self._working_since.keys()):
            if terminal_id not in self._kiro_terminals:
                continue

            working_duration = now - self._working_since[terminal_id]
            if working_duration < _KIRO_WORKING_THRESHOLD:
                continue

            # Read pane and check for permission prompt
            pane_id = self._terminal_to_pane.get(terminal_id)
            if not pane_id:
                continue

            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "pane", "read", pane_id],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                continue

            # Check for kiro permission prompt pattern
            # (WAITING_USER_ANSWER indicator)
            from cli_agent_orchestrator.providers.kiro_cli import TUI_PERMISSION_PATTERN

            if re.search(TUI_PERMISSION_PATTERN, result.stdout):
                logger.info(
                    f"Kiro permission prompt detected for {terminal_id} "
                    f"(working for {working_duration:.0f}s)"
                )
                self._deliver(terminal_id)
                # Reset the timer so we don't spam
                self._working_since[terminal_id] = now

    async def _send(self, message: dict) -> None:
        """Send a JSON message to the herdr socket."""
        assert self._writer is not None
        data = json.dumps(message).encode() + b"\n"
        self._writer.write(data)
        await self._writer.drain()
