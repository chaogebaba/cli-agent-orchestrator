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

**Identity (§7, §9 round 2, and the H1 binding contract).**  There are TWO
identifier namespaces and they are NOT interchangeable:

* the **CAO terminal id** — a UUID, minted by CAO, reaching the pane only as
  ``--env CAO_TERMINAL_ID`` (``backends/herdr_backend.py`` ``_build_env_args``).
  It is what every emitted ``EventDraft`` is attributed to, because it is the
  only id the projector, the state store and ``cao diag`` know.
* the **herdr terminal id** — herdr's own ``term_65b014cd203821``, minted by the
  herdr server and carried on its pane records.  It is what a pane record must
  be MATCHED on, and it is new after every herdr server restart.

Conflating them is not a naming slip, it is a silent no-op: a source constructed
with the CAO uuid and matching pane records on ``pane["terminal_id"]`` binds to
no real pane, so every event is dropped and the cohort looks permanently quiet.
So the binding contract is: **emit on the CAO id, match on the herdr id (or the
pane id), rebind on the stable ``agent_session``.**  herdr's ``terminal_id`` is
new after every server restart, so nothing here keys resume on it.  The stable handle is herdr's
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
import time
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
from cli_agent_orchestrator.core.timing import NO_SIGNAL_S

__all__ = [
    "HERDR_STATUS_TO_EVENT",
    "HerdrRuntimeSource",
    "attach",
    "detach",
    "reset_sources",
    "set_event_loop",
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

#: The broadcast half of this source's subscription.  ``pane.updated`` carries
#: pane METADATA — creation, focus, revision bumps — and nothing else; measured
#: on herdr 0.9.0, an echo into a pane produces zero frames.
_BROADCAST_SUBSCRIPTIONS: list[dict[str, Any]] = [{"type": "pane.updated"}]

#: The one frame that carries a lifecycle transition, spelled the way herdr
#: spells it on the wire — DOTS, not underscores (see :meth:`_handle_event`).
PANE_AGENT_STATUS_CHANGED = "pane.agent_status_changed"


def _subscriptions_for(pane_id: str | None) -> list[dict[str, Any]]:
    """The ONE ``events.subscribe`` this connection may send.

    An agent-status transition does NOT emit ``pane.updated``.  It emits
    ``pane.agent_status_changed``, which is a PER-PANE subscription — sent
    without a ``pane_id`` herdr refuses it with ``missing field 'pane_id'`` and
    closes the connection.  Measured on 0.9.0 (five ``report_agent`` transitions,
    five ``pane.agent_status_changed`` pushes, not one accompanying
    ``pane.updated``); upstream ``ogulcancelik/herdr#2115`` describes it.

    So subscribing to the broadcast alone is a stream that can never carry the
    lifecycle this source exists to read — which is exactly what the H1 live
    round observed as total silence.

    herdr resets the connection on a SECOND ``events.subscribe``, so the per-pane
    spec cannot be added later; it is BATCHED into this one call, which herdr
    accepts.  PR #502's reconnect-storm argument for replacing per-pane specs
    with one broadcast applies to the SHARED ``herdr_inbox_service`` connection,
    not here: this is one source per CAO terminal with its own connection and one
    pane, so there is no storm to cause.
    """
    subscriptions = list(_BROADCAST_SUBSCRIPTIONS)
    if pane_id:
        subscriptions.append({"type": "pane.agent_status_changed", "pane_id": pane_id})
    return subscriptions


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
#: The server's event loop, recorded by the composition root.
#:
#: This is not a convenience.  ``attach`` is called from the backend shim inside
#: ``create_window``, which the terminal service runs on a WORKER THREAD under
#: the lifecycle lock — there is no running loop there, so ``get_running_loop``
#: raises and the source would be created and never started.  A source that never
#: starts never subscribes, so the live cohort would report nothing at all while
#: every unit test (which drives the source by hand) stayed green.  The loop the
#: server actually runs on has to be handed in from the one place that knows it.
_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Record the server's loop so a worker-thread attach can schedule onto it."""
    global _loop
    with _lock:
        _loop = loop


def reset_sources() -> None:
    """Drop and stop every source.  For tests and a re-installed bootstrap."""
    with _lock:
        sources = list(_sources.values())
        _sources.clear()
    for source in sources:
        source.stop_sync()


class HerdrRuntimeSource:
    """Streams one herdr session's ``pane.updated`` events for its bound panes.

    Satisfies ``core.ports.EventSource`` structurally.  One source per CAO
    terminal (§4).  ``terminal_id`` on this object is the **CAO** id — the id
    every emitted draft is attributed to.  The pane it watches is matched on the
    **herdr** id namespace instead: the herdr ``terminal_id`` and/or the herdr
    ``pane_id`` it was attached with, and thereafter the stable
    ``agent_session`` — never assumed stable across a restart.

    A caller that knows only one of the two herdr keys may pass only that one.
    The shim attaches at ``create_window``, where herdr's create response yields
    the ``pane_id`` but not yet herdr's own ``terminal_id``; the herdr terminal
    id is then LEARNED from the first pane record that matches on the pane id,
    and from that point carries provenance and the post-restart ephemeral
    fallback.

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
        cao_terminal_id: str,
        *,
        herdr_terminal_id: str | None = None,
        pane_id: str | None = None,
        herdr_session: str = "cao",
        socket_path: str | None = None,
        client: HerdrClient | None = None,
        reconnect_backoff_base_s: float = 1.0,
        reconnect_backoff_max_s: float = 30.0,
        # A quarter of ``NO_SIGNAL_S``: ``Projector._source_healthy`` treats a
        # probe column older than that horizon as UNHEALTHY and stops muting
        # derived events, which is the right reading of a source that has died
        # and the wrong reading of a herdr source whose worker simply has not
        # changed state for a minute.  Health is a heartbeat, not an event count,
        # and a quarter leaves three missed beats before the projector doubts us.
        # An ARG DEFAULT rather than a module constant, deliberately: §4c forbids
        # a ``*_S``-named module-level duration binding outside ``core/timing.py``
        # (``test_no_other_new_module_defines_a_duration_constant``), and this
        # module's backoff seconds are arg defaults for exactly the same reason.
        probe_keepalive_s: float = NO_SIGNAL_S / 4.0,
        # How long a pushed frame vouches for the stream.  Generous on purpose:
        # herdr's frames are edge-triggered, so a worker that holds one status
        # legitimately pushes nothing, and the question this bound answers is
        # "has the subscription gone dead", not "is the worker busy".  Past it
        # the source stops asserting health rather than asserting a stale one.
        stream_proof_ttl_s: float = NO_SIGNAL_S * 10.0,
        # Does this terminal's cell carry a PASS herdr_certification row for this
        # backend?  The shim knows (it resolves the predicate at create); the
        # adapter must not ask, since `utils` is legacy and an adapter is a leaf.
        # It decides only one thing: whether a PUSHED lifecycle frame is
        # authoritative — see :meth:`_confidence_for`.
        lifecycle_authoritative: bool = False,
    ) -> None:
        if not herdr_terminal_id and not pane_id:
            raise ValueError(
                "a herdr runtime source needs at least one herdr-namespace key "
                "(herdr_terminal_id or pane_id); the CAO terminal id never "
                "matches a herdr pane record"
            )
        #: The **CAO** terminal id.  Every emitted draft is attributed to it, and
        #: it is this source's key in the module registry.  It is NEVER compared
        #: against a herdr pane record.
        self.terminal_id = cao_terminal_id
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
        #: The **herdr** terminal id this source matches pane records on, in
        #: herdr's own namespace (``term_*``).  ``None`` when the attacher knew
        #: only the pane id; learned from the first matching pane record and
        #: re-learned on every stable-session match, so provenance and payload
        #: carry the LIVE ephemeral id while the binding stays on the stable one.
        self._herdr_terminal_id: str | None = herdr_terminal_id
        #: The **herdr** pane id this source matches on when the herdr terminal
        #: id is not (yet) known — the id the shim gets back from herdr's tab
        #: create response.  Re-learned alongside the terminal id.
        self._bound_pane_id: str | None = pane_id
        self._probe_keepalive_s = probe_keepalive_s
        self._keepalive_task: asyncio.Task[None] | None = None
        #: ``time.monotonic()`` of the last PUSHED frame that belonged to this
        #: source.  ``None`` until the subscription delivers one — which is what
        #: makes "never received a frame" distinguishable from "quiet worker".
        self._last_push_at: float | None = None
        self._stream_proof_ttl_s = stream_proof_ttl_s
        self._lifecycle_authoritative = lifecycle_authoritative

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
        self._stop_keepalive()
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
        """End the source from SYNC code, which is always another thread.

        Every caller is a legacy worker thread: ``detach`` runs inside
        ``terminal_service.detach_observation`` under the lifecycle lock, never on
        the server loop.  ``Task.cancel()`` is not thread-safe — it reaches
        ``loop.call_soon``, whose ``_check_thread`` raises ``RuntimeError:
        Non-thread-safe operation invoked on an event loop other than the current
        one``.  That was latent until the source actually ran on a loop; once it
        did, teardown raised, and the raise happened BEFORE the caller dropped
        this terminal's authority, so a deleted terminal stayed authoritative and
        fallback-muted for the life of the process while its task and socket
        leaked.

        ``_stopping`` alone is not enough either: it is only observed at the next
        frame or keepalive tick, and a parked ``async for`` may never reach one.
        So the cancellation is HANDED to the loop, and the client is closed there
        too — ``stop_sync`` previously never closed it at all.
        """
        self._stopping.set()
        task = self._task
        self._task = None
        keepalive = self._keepalive_task
        self._keepalive_task = None
        client = self._client
        self._client = None
        loop = self._loop_of(task) or _installed_loop()
        if loop is None or loop.is_closed():
            # No loop to hand it to: the source was never scheduled (a test
            # driving it by hand), so there is nothing running to cancel.
            return

        def _teardown() -> None:
            for pending in (keepalive, task):
                if pending is not None:
                    pending.cancel()
            if client is not None:
                asyncio.ensure_future(client.close())

        try:
            loop.call_soon_threadsafe(_teardown)
        except RuntimeError:  # pragma: no cover - loop closed under us
            logger.debug(
                "herdr runtime source %s teardown could not reach the loop",
                self.terminal_id,
                exc_info=True,
            )

    @staticmethod
    def _loop_of(task: "asyncio.Task[None] | None") -> asyncio.AbstractEventLoop | None:
        if task is None:
            return None
        try:
            return task.get_loop()
        except RuntimeError:  # pragma: no cover - defensive
            return None

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
        await client.subscribe(_subscriptions_for(self._bound_pane_id))
        logger.debug(
            "herdr runtime source %s subscribed (herdr_terminal_id=%s pane_id=%s)",
            self.terminal_id,
            self._herdr_terminal_id,
            self._bound_pane_id,
        )
        # NOT a health bump.  A live subscription is not evidence that the
        # subscription DELIVERS — r1's defect — so connecting proves nothing and
        # the column stays NULL until a pushed frame arrives.
        self._start_keepalive()
        try:
            # Recipe step 2: snapshot after subscribing, apply current state, THEN
            # stream buffered events.  On a reconnect this is what recovers the gap —
            # events are not receipts, so the snapshot is the source of truth and the
            # event stream only carries changes after it.
            await self._apply_snapshot(client)
            async for event in client.events():
                if self._stopping.is_set():
                    return
                self._handle_event(event)
        except BaseException:
            # Logged HERE as well as in ``_run`` because the two answer different
            # questions: ``_run`` records that the stream ended, this records what
            # the source had bound when it did.  A live round that sees neither
            # events nor gaps is asking exactly that.
            logger.debug(
                "herdr runtime source %s stream ended (herdr_terminal_id=%s pane_id=%s)",
                self.terminal_id,
                self._herdr_terminal_id,
                self._bound_pane_id,
                exc_info=True,
            )
            raise
        finally:
            self._stop_keepalive()

    # -- source health -------------------------------------------------------

    def _touch_source_probe(self) -> None:
        """Bump ``worker_state_shadow.last_source_probe_at`` for this terminal.

        ``Projector._source_healthy`` reads that column and treats NULL as
        UNHEALTHY, so a source that never bumps it is a source the projector
        never believes: ``_is_muted`` returns False for every derived pane event
        and §5's source-level precedence silently never engages.  Nothing in the
        shipped adapter called this, which is why wiring it was a precondition
        for the seam binding at all rather than an optimisation.

        **The column means "the push stream delivered", not "a socket is open",
        and the distinction is the whole of the rule.**  r1 bumped it on connect,
        on the connect-time snapshot, and on an unconditional keepalive.  For a
        file-tailing source that reading is fine; for a PUSH-subscription source
        it inverts the column's meaning — a subscription that is ACKed and
        permanently silent becomes indistinguishable from one delivering every
        transition, so the projector mutes the scraped lifecycle and the terminal
        freezes at whatever the connect snapshot said, forever, while ``cao diag``
        reports that frozen value as authoritative truth.  That is a false-idle
        generator, and the H1 live round produced exactly the silent subscription
        that triggers it.

        So this is called from ONE place — a pushed frame that belongs — and the
        keepalive only re-bumps while the last pushed frame is recent.  A source
        that has never received a frame leaves the column NULL, reads UNHEALTHY,
        and mutes nothing.

        Best-effort by the same rule :func:`~adapters.truth.wiring.emit` follows:
        a diagnostic may never raise into the thing it observes.
        """
        runtime = producer_runtime()
        if runtime is None:
            return
        try:
            store = runtime.state_store
            if store is None:
                # A lane brought producers up without a StateStore.  Strictly
                # less information, never wrong information: the projector then
                # treats the source as unhealthy and the pane fallback stays live.
                return
            store.touch_source_probe(self.terminal_id, probed_at=runtime.clock.now())
        except Exception:  # pragma: no cover - the never-break-the-server rule
            # The guard spans the whole body, not just the call.  A runtime
            # assembled without a ``state_store`` attribute at all (a test
            # double) must cost this producer nothing — a health bump is a
            # diagnostic, and a diagnostic may never raise into the stream it
            # is observing.
            logger.debug(
                "herdr runtime source probe bump failed for %s",
                self.terminal_id,
                exc_info=True,
            )

    def _start_keepalive(self) -> None:
        if self._keepalive_task is not None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - a test driving the source by hand
            return
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name=f"herdr-runtime-keepalive:{self.terminal_id}"
        )

    def _stop_keepalive(self) -> None:
        task = self._keepalive_task
        self._keepalive_task = None
        if task is not None:
            task.cancel()

    async def _keepalive_loop(self) -> None:
        """Re-bump source health while the stream is PROVEN and merely quiet.

        A worker can legitimately hold one status for far longer than
        ``NO_SIGNAL_S``, and herdr's frames are edge-triggered — an idle pane
        pushes nothing.  So a proven stream needs a heartbeat or the projector
        would doubt it for being calm.

        The bound is what keeps that from re-creating the r1 defect: the
        heartbeat only continues while a real frame arrived within
        ``_stream_proof_ttl_s``.  Past that the source stops asserting health and
        the projection degrades, which is the honest reading of a subscription
        that has gone quiet for longer than the worker plausibly has.
        """
        while not self._stopping.is_set():
            await asyncio.sleep(self._probe_keepalive_s)
            if self._stopping.is_set():
                return
            if self._stream_is_proven():
                self._touch_source_probe()

    def _stream_is_proven(self) -> bool:
        """Has a pushed frame arrived recently enough to vouch for the stream?"""
        last = self._last_push_at
        if last is None:
            return False
        return (time.monotonic() - last) <= self._stream_proof_ttl_s

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
                # pushed=False: a snapshot reports a LEVEL, not an edge, and a
                # level the server volunteered on connect is no evidence that the
                # subscription will ever deliver one.
                self._process_pane(pane, pushed=False)

    # -- event handling ------------------------------------------------------

    def _handle_event(self, event: dict[str, Any]) -> None:
        """Route one pushed event.  Only ``pane.updated`` carries lifecycle here.

        The event names itself in ``event`` (underscore form) and nests the pane
        under ``data.pane``; a broadcast ``pane.updated`` is the only kind this
        source subscribes to, so anything else is ignored defensively.
        """
        raw_name = str(event.get("event") or event.get("type") or "")
        # herdr names its two schemas differently in pushed frames, and a blanket
        # underscore->dot rewrite destroys the second family:
        #   EventKind              underscores  pane_updated, pane_created,
        #                                       tab_created, pane_agent_detected
        #   SubscriptionEventKind  dots         pane.agent_status_changed,
        #                                       pane.output_matched
        # ``"pane.agent_status_changed".replace("_", ".")`` is
        # ``pane.agent.status.changed``, so a branch added after the rewrite can
        # never match.  Match the dotted family on the RAW name first.
        if raw_name == PANE_AGENT_STATUS_CHANGED:
            self._handle_agent_status_changed(event)
            return
        if raw_name.replace("_", ".") != "pane.updated":
            return
        data = event.get("data")
        data_dict = data if isinstance(data, dict) else {}
        pane = data_dict.get("pane")
        pane_dict = pane if isinstance(pane, dict) else data_dict
        if not isinstance(pane_dict, dict):
            return
        if not self._pane_belongs(pane_dict):
            logger.debug(
                "herdr runtime source %s ignored a pane: pane_id=%r terminal_id=%r "
                "(bound pane_id=%r herdr_terminal_id=%r session=%r)",
                self.terminal_id,
                pane_dict.get("pane_id"),
                pane_dict.get("terminal_id"),
                self._bound_pane_id,
                self._herdr_terminal_id,
                self._bound_session,
            )
            return
        self._process_pane(pane_dict, pushed=True)

    def _handle_agent_status_changed(self, event: dict[str, Any]) -> None:
        """Route a per-pane ``pane.agent_status_changed`` frame.

        Its payload is FLAT — ``{pane_id, workspace_id, agent, agent_status, …}``
        — with no ``data.pane`` nesting, which :meth:`_pane_belongs` and
        :meth:`_process_pane` already read correctly because both key off
        ``pane_id`` / ``terminal_id`` / ``agent_session`` rather than the wrapper.

        This is the ONLY frame that carries a lifecycle transition.  It is also
        the only frame that counts as proof the push stream is alive — see
        :meth:`_touch_source_probe`.
        """
        data = event.get("data")
        pane = data if isinstance(data, dict) else event
        if not isinstance(pane, dict):
            return
        if not self._pane_belongs(pane):
            logger.debug(
                "herdr runtime source %s ignored an agent_status frame: pane_id=%r "
                "terminal_id=%r (bound pane_id=%r herdr_terminal_id=%r)",
                self.terminal_id,
                pane.get("pane_id"),
                pane.get("terminal_id"),
                self._bound_pane_id,
                self._herdr_terminal_id,
            )
            return
        self._process_pane(pane, pushed=True)

    def _pane_belongs(self, pane: dict[str, Any]) -> bool:
        """Whether this pane record is the one this source's terminal is bound to.

        Every comparison here is in the **herdr** namespace.  ``self.terminal_id``
        — the CAO uuid — is never compared against a pane record: it reaches the
        pane only as an environment variable and appears in no herdr field, so a
        match against it can only ever be False (the shipped conflation, which
        bound no pane and dropped every event).

        The binding key is the STORED stable ``agent_session`` (§7/§9), NOT the
        ephemeral herdr ``terminal_id`` (which is new after every server restart)
        and NOT the pane_id (which does not survive the pane being re-created,
        moved to another workspace, or the server restarting — measured on herdr
        0.9.0: it is retired rather than renumbered).  Two phases:

        * **Before a stable session is bound** the source matches on whichever
          herdr-namespace key it was attached with — herdr's ``terminal_id``, the
          ``pane_id``, or both — and learns that pane's stable ``agent_session``
          as the binding key (:meth:`_bind_session`).  A pane that carries no
          stable session can only ever match here, on those ephemeral keys.
        * **Once a stable session is bound** a pane belongs when its stable
          session equals the bound one, EVEN IF its herdr ``terminal_id`` has
          changed across a restart; the live terminal_id is then re-learned.  A
          pane whose ephemeral keys still match the last-known ones also belongs
          (covers a pane record that omits the session mid-stream), but the
          stable session is authoritative — a DIFFERENT stable session on the
          same terminal_id does NOT belong.
        """
        session = self._session_key(pane)
        if self._bound_session is not None:
            if session is not None:
                return session == self._bound_session
            return self._matches_ephemeral(pane)
        # Unbound: match on the herdr-namespace key(s) we were attached with.
        if not self._matches_ephemeral(pane):
            return False
        self._bind_session(pane, session)
        return True

    def _matches_ephemeral(self, pane: dict[str, Any]) -> bool:
        """Does this pane record match either herdr-namespace key we hold?

        Either key alone is sufficient.  The shim attaches knowing only the pane
        id (herdr's tab-create response yields that and not its terminal id), and
        a test or a reconciler attaching from a snapshot knows the terminal id;
        both must bind, and once either matches the other is learned.
        """
        term = pane.get("terminal_id")
        if self._herdr_terminal_id is not None and term == self._herdr_terminal_id:
            return True
        if self._bound_pane_id is None:
            return False
        return str(pane.get("pane_id") or "") == self._bound_pane_id

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

    def _bind_session(self, pane: dict[str, Any], session: tuple[str, str] | None) -> None:
        """Record the stable session as the binding key on first match.

        Both ephemeral herdr keys are learned here too, so a source attached with
        only one of them carries both from the first matching record onward.
        """
        if session is not None:
            self._bound_session = session
        self._learn_ephemeral(pane)

    def _process_pane(self, pane: dict[str, Any], *, pushed: bool = False) -> None:
        """Map one pane record's ``agent_status`` to a boundary and emit it.

        ``pushed`` says the record came off the SUBSCRIPTION rather than the
        connect-time snapshot.  Only a pushed record proves the stream is
        delivering, and only a pushed record bumps source health — see
        :meth:`_touch_source_probe`.

        Edge-triggered on ``(pane_id, agent_status)``: a repeated status is not a
        new boundary.  A status with no mapping (``blocked``/``unknown``) updates
        the remembered status — so a later real transition is still an edge — but
        emits nothing.
        """
        pane_id = str(pane.get("pane_id") or "")
        status = pane.get("agent_status")
        if not isinstance(status, str) or not status:
            return
        if pushed:
            # Proof of life, whether or not it carries a new boundary: a repeated
            # ``working`` is still the STREAM delivering truth.  Bumped before the
            # edge check so a busy worker reporting the same status for a minute
            # stays healthy.  A SNAPSHOT record is deliberately not proof — see
            # :meth:`_touch_source_probe`.
            self._last_push_at = time.monotonic()
            self._touch_source_probe()
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
        self._learn_ephemeral(pane)

    def _learn_ephemeral(self, pane: dict[str, Any]) -> None:
        """Re-learn the live herdr terminal_id and pane_id for the bound pane."""
        term = pane.get("terminal_id")
        if isinstance(term, str) and term:
            self._herdr_terminal_id = term
        pane_id = pane.get("pane_id")
        if pane_id is not None and str(pane_id):
            self._bound_pane_id = str(pane_id)

    def _confidence_for(self, pane: dict[str, Any]) -> Confidence:
        """How much authority this particular reading carries.

        ``screen_detection_skipped`` is herdr's own signal that a pane's status
        came from a lifecycle hook rather than a screen manifest, and where it
        appears it still decides — that is the SNAPSHOT case and it is unchanged.

        **It cannot decide the pushed case, because pushed frames do not carry
        it.**  A ``pane.agent_status_changed`` frame carries exactly four keys —
        ``agent``, ``agent_status``, ``pane_id``, ``workspace_id`` — in all 43
        frames of this repo's verbatim pi capture
        (``test/fixtures/herdr/pi-events.jsonl``) and in a fresh live capture on
        herdr 0.9.0 (grok-box-010, four driven transitions, union of keys
        identical, field absent from `pane get`, `agent get` and `api snapshot`
        for that pane too).  After the r2 subscription fix those frames are the
        ONLY lifecycle carrier, so reading the field off them returns ``derived``
        every time, by construction.

        That default is not merely conservative on a CERTIFIED terminal, it is
        fatal: §6(ii) mutes the scraped lifecycle, ``turn.*`` is not in
        ``DERIVED_ALWAYS_KINDS``, and the first pushed frame bumps the probe
        column BEFORE its event reaches the projector — so the terminal's own
        authority mutes the terminal's own events and it projects the connect
        snapshot and then nothing, forever, with a perfectly working stream.  The
        same false-idle freeze the health rule was written to kill, reached from
        the other side.

        So on a terminal whose cell is CERTIFIED for this backend, the answer
        comes from the certification rather than from a field herdr does not
        send.  That is not a weaker claim, it is the claim certification makes: a
        PASS row says this source is the authority for this terminal's lifecycle.
        An UNCERTIFIED terminal keeps the field-based reading and stays
        ``derived`` — and nothing mutes its pane, so derived is right there.

        **Authority does not key on whether the record was PUSHED**, and that
        distinction cost a recovery path.  §6 says the only safe recovery from a
        subscription gap is a resnapshot, because events are not receipts.  While
        authority required ``pushed``, a resnapshot's records were DERIVED, so on
        a certified terminal they were muted — the terminal degraded on the gap
        and then stayed degraded through the very resnapshot meant to recover it,
        waiting for a pushed edge that an edge-triggered stream may never send if
        the status did not change across the gap.  ``pushed`` still decides
        proof-of-stream (:meth:`_touch_source_probe`), which is a different
        question: whether the subscription is delivering, not whether this source
        is the authority.
        """
        if pane.get("screen_detection_skipped") is True:
            return Confidence.AUTHORITATIVE
        if self._lifecycle_authoritative:
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
        logger.debug(
            "herdr runtime source %s emitting %s for herdr_status=%s pane=%s",
            self.terminal_id,
            kind.value,
            status,
            pane_id,
        )
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
        # And the stream is no longer proven: the next connection has to earn
        # health with a pushed frame of its own, exactly as the first did.
        self._last_push_at = None

    async def _close_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception:  # pragma: no cover - defensive
                logger.debug("herdr client close failed for %s", self.terminal_id, exc_info=True)


def attach(
    cao_terminal_id: str,
    *,
    herdr_terminal_id: str | None = None,
    pane_id: str | None = None,
    herdr_session: str = "cao",
    socket_path: str | None = None,
    client: HerdrClient | None = None,
    lifecycle_authoritative: bool = False,
) -> HerdrRuntimeSource | None:
    """Create (or return) the herdr runtime source for one CAO terminal.

    ``cao_terminal_id`` is the id every emitted event is ATTRIBUTED to;
    ``herdr_terminal_id`` / ``pane_id`` are the herdr-namespace keys the pane
    records are MATCHED on.  At least one herdr key is required — attaching with
    only the CAO uuid is the shipped defect (it matches no pane record and drops
    every event), so it is refused here rather than failing silently at runtime.

    Idempotent per CAO terminal.  Returns ``None`` when ingestion is off or when
    no herdr key was given, so a caller on the legacy path can attach
    unconditionally and pay nothing when the switch is not set — the same shape
    ``codex_rollout.attach`` has.  Scheduling the async task is left to the
    caller's loop via :meth:`HerdrRuntimeSource.start`; when a running loop exists
    this schedules it, otherwise the source is returned for a test to drive by
    hand.
    """
    if not cao_terminal_id or producer_runtime() is None:
        return None
    if not herdr_terminal_id and not pane_id:
        logger.debug(
            "herdr runtime attach for %s skipped: no herdr-namespace key",
            cao_terminal_id,
        )
        return None
    with _lock:
        existing = _sources.get(cao_terminal_id)
        if existing is not None:
            return existing
        source = HerdrRuntimeSource(
            cao_terminal_id,
            herdr_terminal_id=herdr_terminal_id,
            pane_id=pane_id,
            herdr_session=herdr_session,
            socket_path=socket_path,
            client=client,
            lifecycle_authoritative=lifecycle_authoritative,
        )
        _sources[cao_terminal_id] = source
    _schedule(source)
    return source


def _installed_loop() -> asyncio.AbstractEventLoop | None:
    """The server loop recorded by the composition root, if any."""
    with _lock:
        return _loop


def _schedule(source: HerdrRuntimeSource) -> None:
    """Start the streaming task, from a loop thread OR a worker thread.

    Three cases, and the middle one is the production case:

    * **A loop is running here** — a test, or a caller already on the server
      loop: schedule directly.
    * **No loop here, but the server's loop was recorded** — the real path.
      ``create_window`` runs on a worker thread under the lifecycle lock, so this
      is where every live attach lands.  ``run_coroutine_threadsafe`` hands the
      coroutine to the loop that can actually run it; without this the source is
      built, registered, and silently never subscribes.
    * **Neither** — a unit test with no loop at all: the source stays dormant and
      the test drives ``_process_pane``/``_emit_gap_degraded`` by hand.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        try:
            asyncio.ensure_future(source.start())
        except Exception:  # pragma: no cover - defensive
            logger.debug("could not schedule herdr runtime source", exc_info=True)
        return
    with _lock:
        loop = _loop
    if loop is None or loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(source.start(), loop)
    except Exception:  # pragma: no cover - defensive
        logger.debug("could not schedule herdr runtime source on the server loop", exc_info=True)


def detach(cao_terminal_id: str) -> None:
    """Stop and drop the source for one CAO terminal."""
    with _lock:
        source = _sources.pop(cao_terminal_id, None)
    if source is not None:
        source.stop_sync()


def source_for(cao_terminal_id: str) -> HerdrRuntimeSource | None:
    with _lock:
        return _sources.get(cao_terminal_id)
