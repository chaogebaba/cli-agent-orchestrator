"""The herdr runtime EventSource — Seam A (WP-HERDR H1, blueprint §4, §5, §6, §8).

A ``core.ports.EventSource`` over :class:`~cli_agent_orchestrator.adapters.herdr.client.HerdrClient`.
It subscribes ONCE to herdr's broadcast ``pane.updated`` stream, maps the herdr
per-pane ``agent_status`` onto the closed ``core/states.py`` ``WorkerState`` the
way §5 dictates, and emits ONLY the existing boundary vocabulary through
``wiring.emit`` — it adds no ``EventKind`` and invents no signal.  The projector
turns those boundary events into ``WorkerState`` exactly as it does for the codex
tailer and the claude hooks; this producer's whole job is to say, in the tree's
existing words, what herdr reported.

**The §5 mapping, and why each arrow is what it is (H0 round 2 settled it):**

======================  ==================================  ===========================
herdr ``agent_status``  emitted boundary event              projector ``WorkerState``
======================  ==================================  ===========================
``working``             ``turn.started``                    ``busy``
``idle``                ``turn.ended``                      ``idle``
``done``                ``turn.ended`` (+ ``done`` metadata) ``idle``  (NOT completed)
``blocked``             — nothing —                          (unchanged)
``unknown``             — nothing —                          (unchanged)
gap (subscription loss) ``pane.missing`` + ``NO_SIGNAL``    ``degraded(no_signal)``
======================  ==================================  ===========================

* ``done`` is NOT durable and is NOT completion (§5).  herdr clears it on focus
  and CAO focuses panes, so ``done`` maps to the SAME boundary as ``idle`` — a
  turn ended — and carries a ``degraded_hint``/``done`` note in the payload for
  ``cao diag`` rather than any distinct state.  COMPLETED needs CAO task/result
  evidence, which this producer does not have.
* ``blocked`` maps to NOTHING, on the round-2 measurement (blueprint §9): herdr
  reported ``blocked`` on NEITHER provider across ≥20 turns, so it cannot be the
  §5 source for ``awaiting_input``.  The "who must answer what" fact stays with
  ``question_state.py`` and the existing ``prompt.awaiting`` producer
  (``claude_hooks``); inventing an ``awaiting_input`` here from a signal herdr
  does not emit is exactly the fabrication §5 forbids.
* ``unknown`` maps to nothing per event: herdr reports ``unknown`` for a pane it
  has no agent registered on (e.g. a wrapper foreground process), which is not a
  lifecycle transition.  Genuine SILENCE is a different thing and is handled as
  the gap below.

**The subscription gap (§6).**  During a subscription gap a certified herdr
cohort has NO lifecycle source, and the merged projector will NOT notice on its
own: its ``no_signal`` sweep maxes over probe timestamps that pane probing keeps
fresh, and its pane fallback would silently revert the cohort to scraped
lifecycle.  §6 makes the source itself emit the degraded signal: on a socket
drop this producer appends ``pane.missing`` carrying ``DegradedReason.NO_SIGNAL``
(in the ``degraded_reason`` payload key the projector reads to override the
kind-default reason) at ``authoritative`` confidence — the authoritative source
declaring its OWN loss of signal, which the projector never mutes behind a stale
source-health timestamp — for every pane it was tracking, so the cohort degrades
(and delivery eligibility is vetoed) instead of reverting.  Recovery is a
resnapshot on reconnect — events
are not receipts, so the only safe recovery from a gap is to re-read the whole
state, exactly the herdr client recipe (subscribe → snapshot → apply events).

**Identity (§7, §9 round 2).**  herdr's ``terminal_id`` is new after every server
restart, so nothing here keys resume on it.  The stable handle is herdr's
``agent_session`` (``{kind, source, value}`` — the provider session reference)
together with the pane's ``agent`` name; this producer records that handle in
each event's ``source_ref`` and payload so a later resume rebinds on the stable
id rather than the ephemeral one.  This producer does NOT itself perform resume —
that is §7's flow — it only carries the identity forward.

**What it does NOT own.**  ``process.exited`` has exactly one owner, the liveness
probe (AC4b); a pane that herdr stops reporting is a gap here, never an exit.
``usage.capped`` has no herdr signal and stays a pane/legacy-egress observation.
Delivery (§6 journal) is Seam B's; this is the lifecycle source only.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from cli_agent_orchestrator.adapters.herdr.client import (
    HerdrClient,
    HerdrError,
    HerdrTransportError,
    default_socket_path,
)
from cli_agent_orchestrator.adapters.truth.wiring import emit, producer_runtime
from cli_agent_orchestrator.core.events import (
    Confidence,
    EventDraft,
    EventKind,
    Producer,
)
from cli_agent_orchestrator.core.states import DegradedReason

__all__ = [
    "HERDR_STATUS_TO_EVENT",
    "HerdrRuntimeSource",
    "attach",
    "detach",
    "reset_sources",
    "source_for",
]

logger = logging.getLogger(__name__)

#: The §5 mapping as a table, so the arrows are data a test enumerates rather than
#: branches a test must reach one by one.  A herdr status absent from this map
#: (``blocked``, ``unknown``, or anything a future herdr adds) emits nothing —
#: the SILENT default is deliberate: this producer never fabricates a boundary
#: from a status it was not told maps to one.
HERDR_STATUS_TO_EVENT: dict[str, EventKind] = {
    "working": EventKind.TURN_STARTED,
    "idle": EventKind.TURN_ENDED,
    "done": EventKind.TURN_ENDED,
}

#: herdr statuses that carry unseen-activity metadata onto the mapped event.
#: ``done`` is idle-plus-unseen (§5); recording it in the payload keeps the
#: distinction visible to ``cao diag`` without giving it a state of its own.
_UNSEEN_ACTIVITY_STATUSES = frozenset({"done"})

#: The broadcast subscription this source sends.  A single ``pane.updated`` with
#: NO pane filter, so a pane registered after connect is already covered — the
#: same broadcast shape ``herdr_inbox_service`` uses, and the reason only ONE
#: ``events.subscribe`` is ever sent per connection (herdr resets the connection
#: on a second one).
_SUBSCRIPTIONS: list[dict[str, Any]] = [{"type": "pane.updated"}]

#: Reconnect backoff doubles each failure up to a ceiling.  The MULTIPLIER is a
#: dimensionless ratio, not a duration, so it is a module constant here; the base
#: and ceiling SECONDS are ``__init__`` arg defaults (see :class:`HerdrRuntimeSource`)
#: rather than module constants, because §4c forbids a bare duration literal
#: outside ``core/timing.py`` for a ``*_S``-named binding and the H1 brief forbids
#: editing ``core/`` — an arg default is neither.  Promoting these to
#: ``core/timing.py`` is a noted H2 follow-up (see the module report's Deviations).
_BACKOFF_MULTIPLIER = 2.0

_lock = threading.RLock()
#: terminal_id -> the live source for it (one source per terminal, §4).
_sources: dict[str, "HerdrRuntimeSource"] = {}


def reset_sources() -> None:
    """Drop and stop every source.  For tests and a re-installed bootstrap."""
    with _lock:
        sources = list(_sources.values())
        _sources.clear()
    for source in sources:
        source.stop_sync()


class HerdrRuntimeSource:
    """Streams one herdr session's ``pane.updated`` events for its bound panes.

    Satisfies ``core.ports.EventSource`` structurally.  One source per terminal
    (§4); the ``pane_id`` it cares about is resolved from the pane records the
    events and the snapshot carry, keyed by the terminal's herdr ``terminal_id``
    OR its stable ``agent_session`` — never assumed stable across a restart.

    ``is_authoritative`` is True: for a hook-backed cohort (pi and the other
    hook-backed kinds) herdr's status is the worker's own report, which is what
    source-level precedence reads.  Confidence per EVENT is finer than that: a
    ``screen_detection_skipped`` pane is ``authoritative`` (hook), a
    screen-manifest pane is ``derived``.  The property is the coarse
    source-level declaration; the per-event ``confidence`` records which kind of
    reading each row was.
    """

    def __init__(
        self,
        terminal_id: str,
        *,
        herdr_session: str = "cao",
        socket_path: str | None = None,
        client: HerdrClient | None = None,
        reconnect_backoff_base_s: float = 1.0,
        reconnect_backoff_max_s: float = 30.0,
    ) -> None:
        self.terminal_id = terminal_id
        self._herdr_session = herdr_session
        self._socket_path = socket_path or default_socket_path(herdr_session)
        self._backoff_base_s = reconnect_backoff_base_s
        self._backoff_max_s = reconnect_backoff_max_s
        #: An injected client is how a test drives this without a real socket;
        #: production leaves it None and one is built per (re)connect.
        self._injected_client = client
        self._client: HerdrClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = threading.Event()
        #: The last herdr status this producer emitted a boundary for, per pane.
        #: Edge-triggering: a repeated ``pane.updated`` carrying the same status
        #: is not a new boundary, so it is not a new row.  Keyed by pane_id
        #: because that is what an event carries; the terminal binding is checked
        #: separately.
        self._last_status: dict[str, str] = {}
        #: pane_ids this source has emitted at least one event for — the set a
        #: gap degrades.  A gap with nothing tracked yet degrades this source's
        #: own terminal so the cohort still vetoes delivery.
        self._tracked_panes: set[str] = set()
        #: The stable identity handle last seen for this terminal's pane, carried
        #: into event provenance (§9): "<source>:<value>" of the agent_session.
        self._identity_ref: str | None = None
        #: The STORED stable ``agent_session`` handle this source is bound to
        #: (§7/§9), learned from the FIRST pane that matched by herdr terminal_id
        #: and thereafter the load-bearing binding key.  herdr's ``terminal_id``
        #: is new after every server restart, so once a stable session is known a
        #: pane belongs when its stable session matches EVEN IF the terminal_id
        #: changed — the restart case B1 falsified when binding was terminal_id
        #: alone.  ``None`` until the first bind; a pane with no stable session
        #: can only ever match on the initial terminal_id.
        self._bound_session: tuple[str, str] | None = None
        #: The herdr terminal_id currently associated with the bound session.
        #: Re-learned on every stable-session match so provenance and payload
        #: carry the LIVE ephemeral id while the binding stays on the stable one.
        self._herdr_terminal_id: str = terminal_id

    # -- EventSource ---------------------------------------------------------

    @property
    def name(self) -> str:
        return "herdr_runtime"

    @property
    def is_authoritative(self) -> bool:
        return True

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name=f"herdr-runtime:{self.terminal_id}")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        client = self._client
        self._client = None
        if client is not None:
            await client.close()
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def stop_sync(self) -> None:
        """Signal the loop to end without awaiting it — teardown from sync code."""
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()

    # -- the run loop --------------------------------------------------------

    async def _run(self) -> None:
        """Connect, subscribe, snapshot, stream — reconnecting on a gap.

        A dropped socket is a §6 gap: this loop emits the degraded signal, waits
        a backoff, and reconnects with a resnapshot.  It never treats a gap as an
        exit and never falls back to scraping — degrade is the whole point.
        """
        backoff = self._backoff_base_s
        while not self._stopping.is_set():
            try:
                await self._connect_and_stream()
                backoff = self._backoff_base_s
            except HerdrError as exc:
                # A subscription gap: NO lifecycle source until we reconnect and
                # resnapshot.  Emit the explicit degraded event §6 requires, then
                # back off.  A transport error and a protocol/other herdr error
                # are the same verdict here — the stream is not delivering truth.
                logger.debug(
                    "herdr runtime source for %s lost its stream: %s",
                    self.terminal_id,
                    exc,
                )
                self._emit_gap_degraded()
            except Exception:  # pragma: no cover - the never-break-the-server rule
                logger.debug(
                    "herdr runtime source for %s hit an unexpected error",
                    self.terminal_id,
                    exc_info=True,
                )
                self._emit_gap_degraded()
            finally:
                await self._close_client()
            if self._stopping.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * _BACKOFF_MULTIPLIER, self._backoff_max_s)

    async def _connect_and_stream(self) -> None:
        """One connection's lifetime: connect → protocol → subscribe → snapshot → events."""
        client = self._injected_client or HerdrClient(self._socket_path)
        self._client = client
        await client.connect()
        await client.check_protocol()
        await client.subscribe(_SUBSCRIPTIONS)
        # Recipe step 2: snapshot after subscribing, apply current state, THEN
        # stream buffered events.  On a reconnect this is what recovers the gap —
        # events are not receipts, so the snapshot is the source of truth and the
        # event stream only carries changes after it.
        await self._apply_snapshot(client)
        async for event in client.events():
            if self._stopping.is_set():
                return
            self._handle_event(event)

    async def _apply_snapshot(self, client: HerdrClient) -> None:
        """Seed per-pane status from ``api snapshot`` without replaying history.

        A snapshot is the current state, not a log of transitions: applying it
        emits a boundary only where the pane's status DIFFERS from the last one
        this source emitted (edge-triggered), so a reconnect whose snapshot shows
        the same status the source last saw replays nothing — the resume rule the
        codex tailer states as "starting at EOF, never at 0", in this producer's
        terms.
        """
        try:
            snapshot = await client.snapshot()
        except HerdrError:
            # A snapshot that fails is not fatal to the stream; the events that
            # follow still carry status.  A failure to read initial state is
            # logged and the stream proceeds.
            logger.debug("herdr runtime snapshot failed for %s", self.terminal_id, exc_info=True)
            return
        panes = snapshot.get("panes")
        if not isinstance(panes, list):
            return
        for pane in panes:
            if isinstance(pane, dict) and self._pane_belongs(pane):
                self._process_pane(pane)

    # -- event handling ------------------------------------------------------

    def _handle_event(self, event: dict[str, Any]) -> None:
        """Route one pushed event.  Only ``pane.updated`` carries lifecycle here.

        The event names itself in ``event`` (underscore form) and nests the pane
        under ``data.pane``; a broadcast ``pane.updated`` is the only kind this
        source subscribes to, so anything else is ignored defensively.
        """
        raw_name = event.get("event") or event.get("type") or ""
        event_name = str(raw_name).replace("_", ".")
        if event_name != "pane.updated":
            return
        data = event.get("data")
        data_dict = data if isinstance(data, dict) else {}
        pane = data_dict.get("pane")
        pane_dict = pane if isinstance(pane, dict) else data_dict
        if not isinstance(pane_dict, dict):
            return
        if not self._pane_belongs(pane_dict):
            return
        self._process_pane(pane_dict)

    def _pane_belongs(self, pane: dict[str, Any]) -> bool:
        """Whether this pane record is the one this source's terminal is bound to.

        The binding key is the STORED stable ``agent_session`` (§7/§9), NOT the
        ephemeral herdr ``terminal_id`` (which is new after every server restart)
        and NOT the pane_id (which herdr renumbers).  Two phases:

        * **Before a stable session is bound** the source matches by the herdr
          ``terminal_id`` it was constructed with — the id herdr reports at first
          contact — and learns that pane's stable ``agent_session`` as the
          binding key (:meth:`_bind_session`).  A pane that carries no stable
          session can only ever match here, on the initial terminal_id.
        * **Once a stable session is bound** a pane belongs when its stable
          session equals the bound one, EVEN IF its herdr ``terminal_id`` has
          changed across a restart; the live terminal_id is then re-learned.  A
          pane whose terminal_id still matches the last-known ephemeral id also
          belongs (covers a pane record that omits the session mid-stream), but
          the stable session is authoritative — a DIFFERENT stable session on the
          same terminal_id does NOT belong.
        """
        session = self._session_key(pane)
        if self._bound_session is not None:
            if session is not None:
                return session == self._bound_session
            return pane.get("terminal_id") == self._herdr_terminal_id
        # Unbound: match on the constructor's herdr terminal_id, then bind.
        if pane.get("terminal_id") != self.terminal_id:
            return False
        self._bind_session(pane, session)
        return True

    @staticmethod
    def _session_key(pane: dict[str, Any]) -> tuple[str, str] | None:
        """The stable binding key ``(source, value)`` from a pane's agent_session.

        ``None`` when the pane carries no usable stable session — an un-agented
        pane, or a record that omits it — in which case the caller falls back to
        the ephemeral terminal_id.
        """
        session = pane.get("agent_session")
        if not isinstance(session, dict):
            return None
        source = session.get("source")
        value = session.get("value")
        if isinstance(source, str) and source and isinstance(value, str) and value:
            return (source, value)
        return None

    def _bind_session(
        self, pane: dict[str, Any], session: tuple[str, str] | None
    ) -> None:
        """Record the stable session as the binding key on first match."""
        if session is not None:
            self._bound_session = session
        term = pane.get("terminal_id")
        if isinstance(term, str) and term:
            self._herdr_terminal_id = term

    def _process_pane(self, pane: dict[str, Any]) -> None:
        """Map one pane record's ``agent_status`` to a boundary and emit it.

        Edge-triggered on ``(pane_id, agent_status)``: a repeated status is not a
        new boundary.  A status with no mapping (``blocked``/``unknown``) updates
        the remembered status — so a later real transition is still an edge — but
        emits nothing.
        """
        pane_id = str(pane.get("pane_id") or "")
        status = pane.get("agent_status")
        if not isinstance(status, str) or not status:
            return
        self._remember_identity(pane)
        if pane_id:
            self._tracked_panes.add(pane_id)
        previous = self._last_status.get(pane_id)
        if previous == status:
            return
        self._last_status[pane_id] = status
        kind = HERDR_STATUS_TO_EVENT.get(status)
        if kind is None:
            # blocked/unknown: recorded as the new edge baseline, no boundary.
            return
        self._emit_status_event(pane, pane_id, status, kind)

    def _remember_identity(self, pane: dict[str, Any]) -> None:
        """Record the stable ``agent_session`` handle for §9 resume identity.

        Also re-learns the LIVE herdr ``terminal_id`` for the bound session so a
        post-restart record (new terminal_id, same stable session) carries the
        current ephemeral id in its payload/provenance while the binding key
        itself stays the stable session.
        """
        session = pane.get("agent_session")
        if isinstance(session, dict):
            source = session.get("source")
            value = session.get("value")
            if source and value:
                self._identity_ref = f"{source}:{value}"
        term = pane.get("terminal_id")
        if isinstance(term, str) and term:
            self._herdr_terminal_id = term

    def _confidence_for(self, pane: dict[str, Any]) -> Confidence:
        """Hook-backed panes are authoritative; screen-manifest panes are derived.

        ``screen_detection_skipped`` is herdr's own signal that a pane's status
        came from a lifecycle hook rather than a screen manifest (H0 round 2:
        pi carries ``screen_detection_skipped=true`` /
        ``full_lifecycle_hook_authority``; claude does not).  Absent the field,
        confidence defaults to ``derived`` — the conservative choice, since a
        producer should not claim authority it cannot demonstrate.
        """
        if pane.get("screen_detection_skipped") is True:
            return Confidence.AUTHORITATIVE
        return Confidence.DERIVED

    def _emit_status_event(
        self, pane: dict[str, Any], pane_id: str, status: str, kind: EventKind
    ) -> None:
        runtime = producer_runtime()
        if runtime is None:
            return
        payload: dict[str, Any] = {
            "herdr_status": status,
            "pane_id": pane_id,
            "agent": pane.get("agent"),
            "herdr_terminal_id": pane.get("terminal_id"),
            "state_change_seq": pane.get("state_change_seq"),
        }
        if status in _UNSEEN_ACTIVITY_STATUSES:
            # §5: done -> idle plus unseen-activity metadata, noted as a hint the
            # projector/diag can read; it is NOT a DegradedReason and NOT a state.
            payload["unseen_activity"] = True
            payload["done_hint"] = "herdr_done_is_idle_not_completed"
        emit(
            EventDraft(
                terminal_id=self.terminal_id,
                kind=kind,
                producer=Producer.SERVER,
                confidence=self._confidence_for(pane),
                observed_at=runtime.clock.now(),
                source_ref=self._identity_ref,
                payload=payload,
            )
        )

    def _emit_gap_degraded(self) -> None:
        """Emit the §6 subscription-gap degraded signal for this source.

        One ``pane.missing`` carrying ``DegradedReason.NO_SIGNAL`` per pane this
        source was tracking, attributed to THIS terminal.  If nothing was tracked
        yet (a gap before the first event), the terminal itself is degraded so a
        certified cohort still vetoes delivery during the gap rather than
        silently reverting to scraped lifecycle.  ``pane.missing`` is the merged
        vocabulary's degraded carrier (the liveness probe uses the same kind with
        a reason payload); this producer reuses it rather than inventing a kind.
        """
        runtime = producer_runtime()
        if runtime is None:
            return
        now = runtime.clock.now()
        try:
            emit(
                EventDraft(
                    terminal_id=self.terminal_id,
                    kind=EventKind.PANE_MISSING,
                    producer=Producer.SERVER,
                    # AUTHORITATIVE, not derived: this is the authoritative source
                    # declaring its OWN loss of signal, not a derived pane
                    # observation.  The projector mutes a DERIVED PANE_MISSING
                    # while the source's health timestamp is still fresh
                    # (projector.py `_is_muted`), and pane probing keeps that
                    # timestamp fresh on a herdr pane — so a derived gap event
                    # would be swallowed and the cohort would silently keep its
                    # last live state (B2).  Emitting it AUTHORITATIVE takes the
                    # `confidence is not DERIVED` fast-exit in `_is_muted`, so the
                    # gap is never muted by stale source health.  §6: a certified
                    # cohort in a subscription gap has NO lifecycle source and
                    # MUST degrade rather than revert to scraped lifecycle.
                    confidence=Confidence.AUTHORITATIVE,
                    observed_at=now,
                    source_ref=self._identity_ref,
                    payload={
                        # `degraded_reason` is the payload key the projector's
                        # `_payload_reason` reads to OVERRIDE the kind-default
                        # reason (which for PANE_MISSING is `pane_unreadable`).
                        # Setting it makes the gap project degraded(NO_SIGNAL)
                        # end to end, closing B2's `pane_unreadable`!=`no_signal`
                        # gap.  `reason` is kept for the raw-draft-level assertions
                        # the shipped adapter test already makes.
                        "degraded_reason": DegradedReason.NO_SIGNAL.value,
                        "reason": DegradedReason.NO_SIGNAL.value,
                        "cause": "herdr_subscription_gap",
                        "herdr_terminal_id": self._herdr_terminal_id,
                        "tracked_panes": sorted(self._tracked_panes),
                    },
                )
            )
        except Exception:  # pragma: no cover - the never-break-the-server rule
            logger.debug(
                "herdr runtime gap-degraded emit failed for %s",
                self.terminal_id,
                exc_info=True,
            )
        # After a gap the remembered per-pane statuses are stale; clear them so
        # the resnapshot on reconnect re-emits the true current boundary as an
        # edge rather than suppressing it as a repeat.
        self._last_status.clear()

    async def _close_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception:  # pragma: no cover - defensive
                logger.debug("herdr client close failed for %s", self.terminal_id, exc_info=True)


def attach(
    terminal_id: str,
    *,
    herdr_session: str = "cao",
    socket_path: str | None = None,
    client: HerdrClient | None = None,
) -> HerdrRuntimeSource | None:
    """Create (or return) the herdr runtime source for one terminal.

    Idempotent per terminal.  Returns ``None`` when ingestion is off, so a caller
    on the legacy path can attach unconditionally and pay nothing when the switch
    is not set — the same shape ``codex_rollout.attach`` has.  Scheduling the
    async task is left to the caller's loop via :meth:`HerdrRuntimeSource.start`;
    when a running loop exists this schedules it, otherwise the source is returned
    for a test to drive by hand.
    """
    if not terminal_id or producer_runtime() is None:
        return None
    with _lock:
        existing = _sources.get(terminal_id)
        if existing is not None:
            return existing
        source = HerdrRuntimeSource(
            terminal_id,
            herdr_session=herdr_session,
            socket_path=socket_path,
            client=client,
        )
        _sources[terminal_id] = source
    _schedule(source)
    return source


def _schedule(source: HerdrRuntimeSource) -> None:
    """Start the streaming task when a loop is running; else stay dormant.

    A source with no running loop is created and returned but not started — a
    test drives its ``_process_pane``/``_emit_gap_degraded`` directly, and the
    server schedules ``start`` on its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        asyncio.ensure_future(source.start())
    except Exception:  # pragma: no cover - defensive
        logger.debug("could not schedule herdr runtime source", exc_info=True)


def detach(terminal_id: str) -> None:
    """Stop and drop the source for one terminal."""
    with _lock:
        source = _sources.pop(terminal_id, None)
    if source is not None:
        source.stop_sync()


def source_for(terminal_id: str) -> HerdrRuntimeSource | None:
    with _lock:
        return _sources.get(terminal_id)
