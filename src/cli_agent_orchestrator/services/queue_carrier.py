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

**Nothing here decides what a refusal MEANS.** Every reason string is passed up
verbatim and classified by :func:`core.delivery.classify_wake_reason`, which is
total over the producers' closed set. A bridge that folded reasons together
would be the place an unclassified string acquired a bound by accident.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from cli_agent_orchestrator.core.delivery import (
    AttemptOutcome,
    InjectionResult,
    ReceiverResolution,
    WakeEmission,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LegacyReceiverDirectory",
    "forget_terminal_status",
    "legacy_enqueue_fact",
    "note_terminal_status",
    "queue_owns_new_traffic",
    "queue_runtime",
    "write_through_enqueue",
    "NativeSeatCarrier",
    "PaneWorkerInjector",
    "queue_owns_delivery",
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

    False everywhere but ``on``. ``drain`` accepts no new queue rows at all —
    that is the position's whole point, since it empties on its own budget while
    new enqueues go back to the legacy inbox.
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
    """Is the write-through position live, so D6's surfaces must stay quiet?

    ONE spelling of the mute for the whole legacy tree. Each of K1 through K7
    asks this before it emits, so 3b's single-emitter property rests on the
    SWITCH — in 3c it rests on the deletions, and AC-3c's greps test those.

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
