"""The legacy side of D7's two seams (WP-ARCH phase 3, sub-phase 3b).

``app`` may not import ``services`` — that is the ``new-code-never-imports-legacy``
contract, and it is the whole reason the strangler works. So the delivery tick
talks to three Protocols in ``core.ports`` and THIS module is what satisfies
them, on the sanctioned direction: legacy importing new code, wired together by
the composition root.

Three implementations, and each is a thin bridge rather than a place decisions
live:

* :class:`LegacyReceiverDirectory` — a durable mailbox id to its current
  incarnation, its role and its pane, using the resolution and probe the
  codebase already trusts.
* :class:`NativeSeatCarrier` — the seat's ONLY carrier: ``write_to_socket``,
  server-side, inside the process that holds the rows. Grepped at the phase's
  anchor base this is the only code in the fork that opens a
  ``messagingSocketPath`` or composes a ``<cross-session-message>`` wrapper, so
  A1's carrier is named without ambiguity.
* :class:`PaneWorkerInjector` — D7's worker seam, unchanged, with legacy's two
  vetoes in force and a hard refusal for a supervisor-role target.
* :class:`HerdrPromptInjector` — WP-HERDR Seam B, the SAME port for a certified
  herdr cohort: the digest goes to the runtime's own ``agent.prompt`` instead of
  being typed into a composer.  Fourth implementation in this module, which
  wp-acp-plane §10 calls the intended shape.

Why Seam B lands HERE and not under ``adapters/``: it needs the herdr transport
leaf (``adapters/herdr/client.py``) AND two legacy facts — the CAO terminal's
herdr pane id, which only ``backends`` can resolve, and the role probe.  An
``adapters/`` home could not import either (``adapters-are-leaves``).  On the
legacy side both are ordinary imports, and ``adapters/truth/herdr_runtime.py``
records this same rule in its own comment.

**Nothing here decides what a refusal MEANS.** Every reason string is passed up
verbatim and classified by :func:`core.delivery.classify_wake_reason`, which is
total over the producers' closed set. A bridge that folded reasons together
would be the place an unclassified string acquired a bound by accident.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Optional

from cli_agent_orchestrator.core.delivery import (
    AttemptOutcome,
    InjectionResult,
    LegacyAdoption,
    PersistentEnqueueRejection,
    ReceiverResolution,
    WakeEmission,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AcpTransport",
    "LegacyInboxAdoption",
    "LegacyReceiverDirectory",
    "adopt_enqueue",
    "forget_terminal_status",
    "legacy_enqueue_fact",
    "note_terminal_status",
    "queue_owns_new_traffic",
    "queue_runtime",
    "write_through_enqueue",
    "HerdrPromptInjector",
    "NativeSeatCarrier",
    "PaneWorkerInjector",
    "PersistentEnqueueRejection",
    "queue_owns_delivery",
    "queue_owns_receiver_delivery",
]


#: Per-terminal last latched status, for D8's completion EDGE.
#:
#: The status egress fires on every publish, and D8 is explicit that the cancel
#: is "evaluated once per completion EVENT, not as a standing predicate over
#: ``ready`` rows" — that is what keeps its limit true, since a steer reclaimed
#: to ``ready`` after the completion must NOT be retroactively cancelled. So the
#: edge is detected here rather than by re-running the rule on every tick.
_last_status: dict[str, str] = {}
_status_lock = threading.Lock()

#: The latched statuses that mean the receiver finished its work.
_COMPLETION_STATUSES = frozenset({"completed"})


def note_terminal_status(terminal_id: str, latched_status: object) -> None:
    """D8's trigger: a receiver's own completion cancels its flagged steers.

    Called from the single status egress every origin passes through, so there is
    one trigger rather than one per producer.

    ``supersede_key`` handles the same-mailbox case at enqueue and does NOT reach
    #435, where the aged steer is addressed to the WORKER and the completion
    callback to the supervisor — no newer row ever lands in the worker's mailbox,
    so nothing supersedes the steer. The receiver's own completion is the event
    that does reach it.

    Edge-triggered and fail-silent: a completion that cannot cancel is logged and
    the rows die on their own budget, which is a bounded ending rather than a
    stalled one.
    """
    status = str(getattr(latched_status, "value", latched_status) or "").lower()
    if not terminal_id:
        return
    with _status_lock:
        previous = _last_status.get(terminal_id)
        _last_status[terminal_id] = status
    if status not in _COMPLETION_STATUSES or previous == status:
        return

    try:
        from cli_agent_orchestrator.app.delivery.wiring import record_completion
        from cli_agent_orchestrator.clients.database import SessionLocal, resolve_inbox_receiver

        with SessionLocal() as db:
            _cache, mailbox_id, _generation = resolve_inbox_receiver(db, terminal_id)
        receiver = mailbox_id or terminal_id
        cancelled = record_completion(receiver)
        if cancelled:
            logger.info(
                "d8 completion-cancel terminal=%s receiver=%s cancelled=%d",
                terminal_id,
                receiver,
                len(cancelled),
            )
    except Exception:  # noqa: BLE001 — a cancel may never break a status publish
        logger.debug("d8 completion-cancel failed for %s", terminal_id, exc_info=True)


def forget_terminal_status(terminal_id: str) -> None:
    """Drop a reaped terminal's edge memory, so the map cannot grow unbounded."""
    with _status_lock:
        _last_status.pop(terminal_id, None)


def queue_owns_new_traffic() -> bool:
    """Does the queue own NEW traffic, so the legacy inbox stops inserting? (§6)

    True whenever the queue came up. It used to be false at ``drain`` as well as
    at ``off`` — ``drain`` accepted no new queue rows at all, which was the
    position's whole point, since it emptied on its own budget while new enqueues
    went back to the legacy inbox. WP-ARCH 3c deletes the legacy carriers, so
    there is nothing for new traffic to go back TO and the switch that chose
    between them has one position left.
    """
    try:
        from cli_agent_orchestrator.app.delivery.wiring import queue_owns_new_traffic as _owns

        return _owns()
    except Exception:  # pragma: no cover — an unimportable switch is "not on"
        return False


def write_through_enqueue(fact: Any) -> tuple[int, str] | None:
    """Forward one new message to the queue's write-through (§6).

    A pass-through so ``clients/database.py`` reaches the new tree the way every
    other legacy file does — through THIS module. The AC11 contact surface stays
    one file, which is the property the import-contract tests defend: a reviewer
    reads one bridge to see everything legacy now depends on.
    """
    try:
        from cli_agent_orchestrator.app.delivery.wiring import write_through

        return write_through(fact)
    except Exception:  # noqa: BLE001 — a write-through may never break a send
        logger.debug("wp_arch write_through unavailable", exc_info=True)
        return None


def adopt_enqueue(fact: Any) -> str | None:
    """Forward one EXISTING legacy row to the queue's adoption path (3c).

    The sibling of :func:`write_through_enqueue`, and here for the same reason:
    ``clients/database.py`` reaches the new tree through THIS module only, so the
    AC11 contact surface stays one file a reviewer can read end to end.
    """
    try:
        from cli_agent_orchestrator.app.delivery.wiring import adopt_legacy_row

        return adopt_legacy_row(fact)
    except PersistentEnqueueRejection:
        # The scanner owns durable quarantine and operator diagnostics.  This is
        # the sole exception allowed through the legacy/new-tree bridge.
        raise
    except Exception:  # noqa: BLE001 — retryable failure may never break the tick
        logger.debug("wp_arch adopt_legacy_row unavailable", exc_info=True)
        return None


def legacy_enqueue_fact(**fields: Any) -> Any:
    """Build the fact the write-through takes, without naming the new tree.

    ``clients/database.py`` has the values; only this module knows the type.
    """
    from cli_agent_orchestrator.app.delivery.facts import LegacyEnqueue

    return LegacyEnqueue(**fields)


def queue_runtime() -> Any:
    """The installed delivery runtime, or ``None``.

    ``mailbox_service`` needs the store to serve §5b's drain from the queue, and
    reaches it here rather than importing the wiring module directly.
    """
    try:
        from cli_agent_orchestrator.app.delivery.wiring import delivery_runtime

        return delivery_runtime()
    except Exception:  # pragma: no cover
        return None


def queue_owns_delivery() -> bool:
    """Is the queue the carrier, so D6's surfaces must stay quiet?

    ONE spelling of the mute for the whole legacy tree. Each of K1 through K7
    asked this before it emitted, so 3b's single-emitter property rested on the
    SWITCH — in 3c it rests on the deletions, and AC-3c's greps test those. What
    it asked was "is the resolved position ``on``?"; with the ladder collapsed it
    asks whether the queue came up at all, which is the same question now that
    ``on`` is the only position.

    Never raises. A wiring module that cannot answer leaves legacy behaving
    exactly as it does today, which is the safe direction for a mute: the cost of
    a wrong ``True`` is silence, and silence is the bug this phase exists to
    remove.
    """
    try:
        from cli_agent_orchestrator.app.delivery.wiring import queue_owns_delivery as _owns

        return _owns()
    except Exception:  # pragma: no cover — an unimportable switch is "not on"
        return False


def queue_owns_receiver_delivery(receiver_id: str | None) -> bool:
    """Does the queue own EVERY undelivered row for this receiver?

    WP-ARCH 3c COLLAPSED this to the coarse switch, and the collapse is the point
    rather than a simplification.

    3b needed the row-scoped form because the coarse mute was wrong in one
    direction, and it was the direction that loses messages: at ``on`` the legacy
    inbox stops accepting inserts but does not become EMPTY, so a terminal-wide
    mute stranded every row still in it with no carrier at all. #741 answered
    that by un-muting the legacy carriers for exactly those receivers.

    3c answers it at the source instead. The tick's adoption pass
    (:func:`clients.database.adopt_orphaned_legacy_rows`) pulls every orphaned
    PENDING row into the queue and retires it, so the set the row-scoped
    predicate existed to protect is emptied on a schedule rather than served by a
    second carrier. With no such rows, "the queue owns every undelivered row" and
    "the queue is running" are the same claim, and keeping two spellings of one
    claim is how they drift apart. (The second half of that sentence used to read
    "the position is ``on``"; the switch collapsed to that one position when the
    legacy carriers it chose between were deleted.)

    ``receiver_id`` is accepted and ignored, deliberately: the callers are
    legacy mute sites that pass what they have, and a signature change would
    touch three modules that all die in slice 3 anyway.

    Never raises: an unanswerable switch reports "not on", which leaves legacy
    behaving as it does today.
    """
    return queue_owns_delivery()


class LegacyInboxAdoption:
    """Satisfies :class:`core.ports.LegacyInboxAdopter` (WP-ARCH 3c).

    A thin bridge, like the three beside it: the tick owns WHEN, this owns the
    one thing ``app`` cannot do, which is read and write the legacy ``inbox``
    table. All the ordering reasoning lives with the function it calls, in
    ``clients/database.py``, because that is where the transactions are.
    """

    def adopt_orphans(self, *, limit: int) -> list[LegacyAdoption]:
        from cli_agent_orchestrator.clients.database import adopt_orphaned_legacy_rows

        try:
            rows = adopt_orphaned_legacy_rows(limit=limit)
        except Exception:  # noqa: BLE001 — a net that raises is not a net
            logger.debug("wp_arch adoption pass failed", exc_info=True)
            return []
        return [
            LegacyAdoption(legacy_message_id=legacy_id, msg_id=msg_id, receiver_id=receiver_id)
            for legacy_id, msg_id, receiver_id in rows
        ]


class LegacyReceiverDirectory:
    """Where a mailbox id lives right now, and what role it plays.

    ``receiver_id`` is the durable MAILBOX id, which is what makes #33
    closeable: a fresh supervisor incarnation is a new generation of the same
    mailbox and inherits the pending rows and the open digest, rather than
    starting behind an empty registry. Resolution to a live terminal happens
    here, at injection time, through the resolver legacy already uses.

    Zero live terminals resolves to a resolution with no ``terminal_id``: the
    digest stays open, the attempt writes ``pane_absent``, and the rows age
    toward ``delivery_dead`` on their own budget (D10). More than one is not
    representable — a mailbox has one current incarnation by construction.
    """

    def resolve(self, receiver_id: str) -> ReceiverResolution:
        from cli_agent_orchestrator.clients.database import SessionLocal, resolve_inbox_receiver

        terminal_id = ""
        try:
            with SessionLocal() as db:
                cache, _mailbox_id, _generation = resolve_inbox_receiver(db, receiver_id)
                terminal_id = cache or ""
        except Exception:
            # A resolver that cannot answer is a receiver with no live
            # incarnation, not an exception the tick has to interpret. The rows
            # then take pane_absent and die on their own budget, which is a
            # bounded ending rather than a stalled one.
            logger.debug("delivery: could not resolve receiver %s", receiver_id, exc_info=True)
            return ReceiverResolution(receiver_id=receiver_id)

        return ReceiverResolution(
            receiver_id=receiver_id,
            terminal_id=terminal_id,
            is_supervisor=self._is_supervisor(terminal_id),
            pane_present=self._pane_present(terminal_id),
            display_name=self._display_name(terminal_id),
        )

    @staticmethod
    def _is_supervisor(terminal_id: str) -> bool:
        from cli_agent_orchestrator.services.mailbox_service import probe_supervisor_role

        return probe_supervisor_role(terminal_id)

    @staticmethod
    def _pane_present(terminal_id: str) -> bool:
        """D10's second conjunct, read from a stored fact rather than inferred.

        ``pane_present`` is maintained every ``PANE_HEARTBEAT_S`` independently
        of delivery, so it is available whether or not anything was injected —
        which matters because an epoch whose messages were all
        completion-cancelled while still ``ready`` reaches terminal state with
        nothing ever injected.

        Falls back to "the terminal row exists and has not been reaped" when the
        projection is unavailable, since phase 1's ingestion switch is
        independent of this phase's and a deployment may be running the queue
        with the projection off. The fallback is weaker evidence and it is the
        reason ``abandoned`` is the closure that can be wrong in the safe
        direction: it says nobody was there, and a live receiver's rows would
        have been consumed rather than reaching this test at all.
        """
        if not terminal_id:
            return False
        try:
            from cli_agent_orchestrator.clients.database import SessionLocal
            from cli_agent_orchestrator.models.terminal import Terminal

            with SessionLocal() as db:
                row: Any = db.query(Terminal).filter_by(id=terminal_id).one_or_none()
                if row is None:
                    return False
                status = str(getattr(row, "status", "") or "")
                return status not in {"terminated", "deleted", "failed"}
        except Exception:
            logger.debug("delivery: pane probe failed for %s", terminal_id, exc_info=True)
            return False

    @staticmethod
    def _display_name(terminal_id: str) -> str:
        try:
            from cli_agent_orchestrator.services.terminal_service import get_terminal_metadata

            metadata = get_terminal_metadata(terminal_id) or {}
            return str(metadata.get("agent_name") or metadata.get("name") or terminal_id)
        except Exception:
            return terminal_id


class NativeSeatCarrier:
    """The seat's wake, over the native cross-session channel. No pane, ever.

    Resolve, version guard, write, verify — the same four steps the doorbell's
    native ring takes, reached through the same functions, because A1 changes the
    CARRIER of the wake and not the transport. What is different is what the
    steps are allowed to refuse: the staleness gate is demoted inside
    ``resolve_target`` itself, so both this caller and the doorbell inherit the
    demotion and an idle seat is woken in every switch position (§A1.5).
    """

    def emit(
        self,
        *,
        terminal_id: str,
        line: str,
        sender_key: str,
        sender_name: str,
        msg_id: str,
    ) -> WakeEmission:
        from cli_agent_orchestrator.services.cc_session_registry import (
            check_version_guard,
            read_peer_token,
            resolve_target,
            verify_wake,
            write_to_socket,
        )
        from cli_agent_orchestrator.services.terminal_service import get_terminal_metadata

        metadata = get_terminal_metadata(terminal_id) or {}
        tmux_session = str(metadata.get("tmux_session", "") or "")
        tmux_window = str(metadata.get("tmux_window", "") or "")
        if not metadata:
            return WakeEmission(reason="no_terminal_metadata")
        if not tmux_session or not tmux_window:
            return WakeEmission(reason="no_tmux_coordinates")

        result = resolve_target(terminal_id, tmux_session, tmux_window)
        annotations: tuple[str, ...] = ("record_stale",) if result.stale else ()
        if result.refusal_reason:
            return WakeEmission(reason=result.refusal_reason, annotations=annotations)
        record = result.record
        if record is None:  # pragma: no cover — guaranteed by no refusal_reason
            return WakeEmission(reason="no_registry_records", annotations=annotations)

        version_refusal = check_version_guard(record)
        if version_refusal:
            return WakeEmission(reason=version_refusal, annotations=annotations)

        socket_path = record.messaging_socket_path or ""
        if not socket_path:
            # The session is registered but has published no socket. It returns
            # when the session does, so this is the deadline bound rather than
            # the attempt budget (§A1.4).
            return WakeEmission(reason="socket_unpublished", annotations=annotations)

        payload = self._envelope(
            line=line, sender_key=sender_key, sender_name=sender_name, msg_id=msg_id
        )
        pre_status = record.status_updated_at or ""
        token: Optional[str] = None
        try:
            token = read_peer_token(record.pid, expected_proc_start=record.proc_start)
        except Exception:  # pragma: no cover — an unreadable token is not a refusal
            token = None

        error = write_to_socket(socket_path, payload, auth_token=token)
        if error is not None:
            return WakeEmission(reason=error, annotations=annotations)

        verified = False
        try:
            verified = bool(verify_wake(record, pre_status))
        except Exception:
            logger.debug("delivery: wake verification raised", exc_info=True)
        return WakeEmission(reason=None, verified=verified, annotations=annotations)

    @staticmethod
    def _envelope(*, line: str, sender_key: str, sender_name: str, msg_id: str) -> str:
        """The msgV:1 envelope, keyed by the DIGEST rather than by an inbox row.

        ``build_wake_payload`` and ``build_wake_msg_id`` both key on a single
        inbox row today — the payload takes ``(worker_name, inbox_row_id, …)``
        and the id is deterministic in worker, row id and incarnation — while
        A1.1's key is receiver, epoch and wake ordinal and carries no row id at
        all. So this is the thin wrapper §13b names: it supplies the digest's key
        and the composed line, and the envelope and socket framing below it are
        untouched, which is the part worth keeping. A builder reading "kept"
        alone would reuse a key tuple the design has replaced.
        """
        import json

        from cli_agent_orchestrator.services.cc_session_registry import _sanitize_sender_name
        from cli_agent_orchestrator.services.config_service import ConfigService

        sender = _sanitize_sender_name(sender_key)
        display = _sanitize_sender_name(sender_name)
        priority = ConfigService.get("supervisor.wake.priority", default="next")
        wrapper = (
            f'<cross-session-message from="bridge:cao-{sender}" from-mode="bridge" '
            f'from-name="{display}" summary="cao delivery digest">\n{line}\n'
            f"</cross-session-message>"
        )
        return json.dumps(
            {
                "v": 1,
                "type": "message",
                "from": f"bridge:cao-{sender}",
                "from-name": display,
                "msg_id": msg_id,
                "priority": priority,
                "message": {"role": "user", "content": wrapper},
            },
            separators=(",", ":"),
        )


class PaneWorkerInjector:
    """D7's worker seam — the composer paste, unchanged for worker receivers.

    The role probe is re-asserted at ENTRY and a supervisor target is refused
    rather than pasted. That refusal is not defensive tidiness: it is what makes
    K8's kill a property of the call graph. A future caller that routes a seat
    row here gets ``paste_attempted``, a finding and a row that dies on the
    attempt budget, instead of a silent paste into a human's composer.
    """

    def inject(self, *, terminal_id: str, line: str) -> InjectionResult:
        from cli_agent_orchestrator.services.mailbox_service import probe_supervisor_role

        if probe_supervisor_role(terminal_id):
            return InjectionResult(outcome=AttemptOutcome.PASTE_ATTEMPTED, detail="paste_attempted")

        from cli_agent_orchestrator.services.inbox_service import inbox_service

        try:
            if inbox_service._dialog_gate_active(terminal_id):
                # Retains the lease and writes its attempt row; the row is
                # bounded by DELIVERY_VETO_CEILING_S rather than by the attempt
                # budget, because a worker waiting on a dialog card and a poison
                # message are different conditions (D12).
                return InjectionResult(outcome=AttemptOutcome.VETO_DIALOG, detail="dialog_gate")
        except Exception:
            # The legacy gate itself fails CLOSED on a probe exception, and so
            # does this bridge: an unreadable gate is a hold, not a paste.
            logger.debug("delivery: dialog gate probe raised for %s", terminal_id, exc_info=True)
            return InjectionResult(outcome=AttemptOutcome.VETO_DIALOG, detail="gate_unreadable")

        return self._send(terminal_id, line)

    @staticmethod
    def _send(terminal_id: str, line: str) -> InjectionResult:
        """D7's seam, and the typed refusals legacy already raises out of it.

        The mapping is the point, so it is written as one table rather than a
        catch-all. Each arm lands the row on the bound D12 gives that condition,
        and the default is ``veto_unverified`` — the attempt budget — because a
        probe that cannot be verified is a FAILING delivery and not a deferral.
        """
        from cli_agent_orchestrator.models.terminal import TerminalInputBlockedError
        from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
        from cli_agent_orchestrator.services.terminal_service import send_prepared_input

        try:
            send_prepared_input(terminal_id, line, defer_on_dialog=True)
        except ValueError:
            # The terminal row is gone: no pane to write to. Attempt budget, and
            # the digest stays open until D10 closes it (case 5).
            return InjectionResult(outcome=AttemptOutcome.PANE_ABSENT, detail="no_terminal")
        except TerminalInputBlockedError:
            # Waiting on a user answer is a dialog hold, not a failure.
            return InjectionResult(outcome=AttemptOutcome.VETO_DIALOG, detail="waiting_user_answer")
        except DeliveryDeferredError as exc:
            return InjectionResult(outcome=AttemptOutcome.VETO_UNVERIFIED, detail=f"deferred:{exc}")
        except Exception as exc:
            logger.debug("delivery: paste failed for %s", terminal_id, exc_info=True)
            return InjectionResult(
                outcome=AttemptOutcome.VETO_UNVERIFIED,
                detail=f"safety_unverified:{exc.__class__.__name__}",
            )
        return InjectionResult(outcome=AttemptOutcome.DELIVERED, detail="pane")


# ---------------------------------------------------------------------------
# WP-HERDR Seam B — the same port, a runtime submission instead of a paste.
# ---------------------------------------------------------------------------


def _herdr_client(socket_path: str) -> Any:
    """The one place Seam B constructs a herdr transport.

    A module-level function rather than an inline constructor so a test can
    substitute the transport without a real unix socket, and so that a reader
    grepping for "who opens a herdr connection on the delivery path" finds one
    answer.  The import is deferred for the reason every import in this module
    is: the composition root must be able to build the tick without pulling the
    adapter tree in.
    """
    from cli_agent_orchestrator.adapters.herdr.client import HerdrClient

    return HerdrClient(socket_path)


def _run_blocking(coro: Any, timeout_s: float) -> Any:
    """Run one coroutine to completion from a SYNCHRONOUS caller, on its own loop.

    ``PaneInjector.inject`` is synchronous and is called from inside the server's
    event loop (``DeliveryTick._run`` awaits nothing around ``run_once``), so
    ``asyncio.run`` here would raise "cannot be called from a running event
    loop" and ``run_coroutine_threadsafe`` onto that same loop would deadlock —
    the loop is the thread that is blocked waiting.  A fresh thread with a fresh
    loop is the one shape that works from both a live loop and a plain test.

    A thread per injection rather than a pool: the tick serves receivers one at a
    time at a ten-second cadence, so the rate is trivial, and a pool whose single
    worker is stuck on a hung socket would stall every later injection behind it.
    The thread is a daemon and is JOINED with a bound — a submission that
    outlives its bound is reported, never waited on forever, and the delivery
    tick keeps its liveness.

    Blocking the loop for the duration is the pre-existing shape, not a new cost:
    ``PaneWorkerInjector._send`` blocks it on ``send_prepared_input`` today.

    **On the timeout path this LEAKS the thread, deliberately** (review r1
    §10.7).  ``asyncio.run`` cannot be interrupted from outside, and the socket
    work it is doing belongs to a loop this thread does not own, so the only ways
    to reclaim it are to thread a cancellation token through every awaited call
    or to kill the loop from under it.  Both are more machinery than the
    condition deserves: the thread is a daemon, it holds one unix socket and one
    reply buffer, it ends on its own as soon as herdr answers or its own socket
    timeout fires (bounded by ``request_timeout_s``, which is well under this
    join), and the process is the outer bound in the worst case.  What it must
    NOT do is come back and write anything — it cannot: its result is dropped on
    the floor by the join above, and the injector has held no per-terminal state
    since r2, so a late thread has nothing to race with.
    """
    import asyncio

    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 — carried, not swallowed
            box["error"] = exc

    thread = threading.Thread(target=runner, name="herdr-inject", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise TimeoutError(f"herdr submission did not finish within {timeout_s}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class HerdrPromptInjector:
    """Seam B: hand the digest to herdr's ``agent.prompt`` (WP-HERDR §4, H2-S3).

    The same ``core.ports.PaneInjector`` the paste implements — the port is NOT
    widened (wp-acp-plane §10 forbids it), so the A2 fenced owner calls one
    method and never learns which carrier ran.

    **The supervisor refusal is re-asserted at entry**, exactly as
    :class:`PaneWorkerInjector` does and for the same reason: K8's kill is a
    property of the call graph, and a dispatch defect that routed a seat row here
    must be loud.  A herdr submission into a human's own pane would be worse than
    a paste, not better, because the runtime would press Enter.

    **The no-second-submission rule is NOT here, and that is the r2 correction.**
    r1 implemented it as a per-TERMINAL block that cleared itself once the pane's
    ``state_change_seq`` advanced, and review r1 §2 showed the property does not
    hold: this injector is handed ``(terminal_id, line)`` and never learns a
    ``msg_id``, the digest it submits covers MANY ids at once, and the clearing
    condition — the pane moved — is the condition under which the first copy most
    likely LANDED.  A stalled submission was therefore re-offered by ``reclaim``
    and submitted a second time, journalled ``delivered``.

    Blueprint amendment (7) rules the quarantine is PER ID and must hold through
    ANY path, so it lives where ids exist: ``SqliteQueueStore.reclaim`` does not
    return a row whose last outcome was ``SUBMISSION_UNCERTAIN`` to the claimable
    pool at all.  That is strictly stronger than anything expressible here, and
    it removes this class's marker, its lock and both of the wedges the marker
    produced (review r1 §4) by construction rather than by another rule.

    What remains here is a pure function of one call: resolve the pane, submit
    once, project the answer.  No state between injections, so there is nothing
    to wedge, nothing to persist, and nothing that a server restart forgets.
    """

    # -- the port -----------------------------------------------------------

    def inject(self, *, terminal_id: str, line: str) -> InjectionResult:
        from cli_agent_orchestrator.services.mailbox_service import probe_supervisor_role

        if probe_supervisor_role(terminal_id):
            return InjectionResult(outcome=AttemptOutcome.PASTE_ATTEMPTED, detail="paste_attempted")

        try:
            target, socket_path = self._resolve(terminal_id)
        except Exception as exc:  # noqa: BLE001
            # No herdr pane for this terminal is the same fact the paste seam
            # calls ``no_terminal``: nothing to write to, attempt budget.
            logger.debug("herdr inject: no target for %s", terminal_id, exc_info=True)
            return InjectionResult(
                outcome=AttemptOutcome.PANE_ABSENT,
                detail=f"herdr:no_target:{exc.__class__.__name__}",
            )

        try:
            return self._submit(target, socket_path, line)
        except TimeoutError:
            # The submission outlived its own bound AFTER the request went out,
            # so it may have landed: uncertain, not failed.  The store's
            # quarantine is what stops the row being offered again.
            return InjectionResult(
                outcome=AttemptOutcome.SUBMISSION_UNCERTAIN, detail="herdr:inject_timeout"
            )
        except Exception as exc:  # noqa: BLE001 — a carrier may not raise into the tick
            logger.debug("herdr inject failed for %s", terminal_id, exc_info=True)
            return InjectionResult(
                outcome=AttemptOutcome.VETO_UNVERIFIED,
                detail=f"herdr:unverified:{exc.__class__.__name__}",
            )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _resolve(terminal_id: str) -> tuple[str, str]:
        """The CAO terminal id to (herdr target, herdr socket path).

        Two namespaces meet here and nowhere else.  The backend owns the pane
        map — it is the thing that created the pane and the only code that can
        rebuild the map from a snapshot — and it also owns which herdr SESSION
        the server is running, which decides the socket.  Both come off the live
        backend rather than from configuration, so a server started with
        ``--terminal herdr --herdr-session X`` cannot be submitted to on session
        ``cao``'s socket.
        """
        from cli_agent_orchestrator.adapters.herdr.client import default_socket_path
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.services.terminal_service import get_terminal_metadata

        backend = get_backend()
        session = getattr(backend, "herdr_session", None)
        if not isinstance(session, str):
            raise RuntimeError("active terminal backend is not herdr")
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"terminal {terminal_id} not found")
        pane_id = backend.get_pane_id(
            terminal_id, metadata.get("tmux_session", ""), metadata.get("tmux_window", "")
        )
        if not pane_id:
            raise ValueError(f"terminal {terminal_id} has no herdr pane")
        return pane_id, default_socket_path(session)

    @staticmethod
    def _submit(target: str, socket_path: str, line: str) -> InjectionResult:
        """One submission, and the detail string passed through unchanged.

        r1 wrote ``f"herdr:{submission.detail}"`` onto a detail the client had
        already prefixed, so every live row read ``herdr:herdr:working`` (review
        r1 §5/§10.4).  The client is now the single place the prefix is applied
        — every detail it returns carries exactly one — which also makes
        ``detail LIKE 'herdr:%'`` the exact predicate for "this row went through
        Seam B", the observability the shared ``carrier=pane`` value costs.
        """
        from cli_agent_orchestrator.core.timing import (
            DELIVERY_INJECT_BUDGET_S,
            HERDR_PROMPT_WAIT_MS,
        )

        submission = _run_blocking(
            _herdr_client(socket_path).prompt_agent(
                target=target, text=line, wait_timeout_ms=HERDR_PROMPT_WAIT_MS
            ),
            DELIVERY_INJECT_BUDGET_S,
        )
        return InjectionResult(outcome=submission.outcome, detail=submission.detail)


class AcpTransport:
    """WP-ACP-PLANE D16/B1 — the seat's wake over the ACP message plane.

    The FOURTH implementation in this module, beside ``NativeSeatCarrier``,
    ``PaneWorkerInjector`` and H2's ``HerdrPromptInjector``.  The blueprint's
    collision row calls three implementations in one module "the intended
    shape", and this is the fourth of the same kind: one port, one transport, no
    conditional inside another carrier.

    **It satisfies ``SeatCarrier``, not ``PaneInjector``, and the difference is
    the point.**  D7 made those separate ports so K8's kill is a property of the
    call graph: the seat branch holds no reference to the pane injector at all,
    so no conditional inside an injector can be got wrong later.  AC-S1.7 asks
    that no ``send_keys`` path be REACHABLE for an ACP seat, and the way to make
    that checkable is for this class to have no way to reach one — not for it to
    decline.

    **No pane, no coordinates, no registry.**  ``NativeSeatCarrier`` reads
    ``tmux_session``/``tmux_window`` and refuses ``no_tmux_coordinates`` without
    them; for a ``transport='acp'`` row those columns are NULL BY DESIGN (D20),
    which is exactly why a per-terminal dispatch chooses between the two rather
    than one carrier learning to branch.

    The submission itself is the plane's own rule: **CAO tracks mid-turn from its
    own stream and never writes a second prompt into an open turn.**  ACP has no
    wire busy class — S0's AC-S0.3c/S0.4 disproved the ``-32003`` model — so a
    busy receiver is reported ``acp_busy_retry``, the row keeps its lease, no
    attempt is spent, and it is delivered on the first ``stopReason`` through
    ``DeliveryTick.nudge`` or, if the driver's stream is severed, by the ordinary
    reclaim floor (AC-S1.3, AC-S1.14).
    """

    #: The typed refusal for a receiver whose turn is still open.  A string
    #: rather than an exception for the reason every other carrier reason is one:
    #: ``classify_wake_reason`` turns carrier reasons into outcomes, and a
    #: carrier that raised would make "busy" an error the tick had to interpret.
    BUSY_REASON = "acp_busy_retry"

    #: No session bound yet for this terminal.  Returns when the seat's first
    #: call does, so it is bounded by the row's own deadline rather than by the
    #: attempt budget — the same reading ``wake_unreachable`` gets.
    UNBOUND_REASON = "acp_session_unbound"

    def __init__(self, sessions: Callable[[str], Any] | None = None) -> None:
        """``sessions`` resolves a terminal id to its live ACP client.

        Injected rather than looked up, so this class holds no registry of its
        own and a test can hand it a mock agent's client.  The composition root
        supplies the real resolver, which is the one place that knows how seats
        are spawned.
        """
        self._sessions = sessions

    def emit(
        self,
        *,
        terminal_id: str,
        line: str,
        sender_key: str,
        sender_name: str,
        msg_id: str,
    ) -> WakeEmission:
        """Write ONE prompt, or report a typed reason.  Never a second prompt."""
        del sender_key, sender_name, msg_id  # the ACP envelope carries its own id
        client = self._sessions(terminal_id) if self._sessions is not None else None
        if client is None:
            return WakeEmission(reason=self.UNBOUND_REASON)

        state = client.session_state()
        if state.session_id is None:
            return WakeEmission(reason=self.UNBOUND_REASON)
        if state.turn_open:
            # The decision AC-S1.3 asserts: the row is NEVER SUBMITTED, so no
            # second ``session/prompt`` leaves CAO.  Reported, not raised, and
            # not queued here — the queue is the tick's, and holding a line in
            # this object would be a second queue nobody could see.
            return WakeEmission(reason=self.BUSY_REASON, detail="turn_open")

        try:
            client.prompt(line)
        except Exception as exc:  # noqa: BLE001 — a transport fault is a typed reason
            # A refused prompt is this client's own busy guard firing on a race
            # between the check above and the write; everything else is the
            # transport. Both are reasons, never exceptions the tick must catch.
            reason = (
                self.BUSY_REASON
                if exc.__class__.__name__ == "PromptRefused"
                else "acp_write_failed"
            )
            return WakeEmission(reason=reason, detail=exc.__class__.__name__)

        # ``verified`` is TRUE here and that is a narrower claim than it looks:
        # the local write receipt survived, which is what D5 calls "accepted".
        # It is not an acknowledgement from the agent, and the design says so
        # rather than letting a flushed pipe stand in for a read message.
        return WakeEmission(verified=True, detail="acp")
