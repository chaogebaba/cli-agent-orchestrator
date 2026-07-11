"""Delivers queued inbox messages when terminals become ready.

Consumer: terminal.{id}.status
"""

import asyncio
import logging
import json
import threading
from itertools import groupby

from cli_agent_orchestrator.backends.base import TerminalNotFoundError
from cli_agent_orchestrator.clients.database import (
    begin_delivery_attempt,
    confirm_batch_from_prior_attempt,
    count_ambiguous_attempts,
    create_inbox_message,
    get_terminal_metadata,
    get_pending_messages,
    list_pending_receiver_ids_by_provider,
    list_pending_receiver_ids_older_than,
    update_message_status,
    settle_delivery_attempt,
    list_stale_delivering_messages,
    get_message_trace,
    list_attempt_member_ids,
    list_message_attempts,
    transition_pending_to_delivery_failed,
)
from cli_agent_orchestrator.constants import (
    EAGER_INBOX_DELIVERY,
    INBOX_RECONCILE_GRACE_SECONDS,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.terminal_service import TerminalInputBlockedError
from cli_agent_orchestrator.services.message_trace_service import (
    confirm_delivery, resolve_session_transcript, transcript_lookup, transcript_ref, wire_hash,
)
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

_delivery_locks: dict[str, threading.Lock] = {}
_delivery_locks_guard = threading.Lock()
_delivery_wake_seq: dict[str, int] = {}
_delivery_seq_guard = threading.Lock()


def _get_delivery_lock(terminal_id: str) -> threading.Lock:
    with _delivery_locks_guard:
        lock = _delivery_locks.get(terminal_id)
        if lock is None:
            lock = threading.Lock()
            _delivery_locks[terminal_id] = lock
        return lock


def clear_terminal_delivery_state(terminal_id: str) -> None:
    """Remove the receiver lock and its wake sequence together on teardown."""
    with _delivery_locks_guard:
        _delivery_locks.pop(terminal_id, None)
        with _delivery_seq_guard:
            _delivery_wake_seq.pop(terminal_id, None)


def _should_defer_waiting(terminal_id: str, provider=None) -> bool:
    status = status_monitor.get_status(terminal_id)
    if status != TerminalStatus.WAITING_USER_ANSWER:
        return False
    if provider is None:
        provider = provider_manager.get_provider(terminal_id)
    return (
        provider is not None
        and getattr(provider, "blocks_orchestrated_input_while_waiting_user_answer", False)
        is True
    )


def _defer_messages(terminal_id: str, messages) -> None:
    for message in messages:
        update_message_status(message.id, MessageStatus.PENDING)
    logger.info(
        "Deferred %s message(s) for terminal %s because a user dialog is active",
        len(messages),
        terminal_id,
    )


class InboxService:
    """Delivers one pending message per terminal per IDLE cycle."""

    def __init__(self) -> None:
        self._defer_attempts: dict[int, int] = {}
        self._defer_notified: set[int] = set()
        self._defer_lock = threading.Lock()

    def _evict_defer_state(self, messages) -> None:
        with self._defer_lock:
            for message in messages:
                self._defer_attempts.pop(message.id, None)
                self._defer_notified.discard(message.id)

    def _record_delivery_deferred(self, terminal_id: str, messages) -> None:
        notify_ids: list[int] = []
        with self._defer_lock:
            for message in messages:
                attempts = self._defer_attempts.get(message.id, 0) + 1
                self._defer_attempts[message.id] = attempts
                if attempts == 5 and message.id not in self._defer_notified:
                    self._defer_notified.add(message.id)
                    notify_ids.append(message.id)

        if not notify_ids:
            return
        try:
            metadata = get_terminal_metadata(terminal_id)
        except Exception:
            metadata = None
            logger.warning(
                "Could not read caller metadata for deferred delivery to terminal %s",
                terminal_id,
                exc_info=True,
            )
        caller_id = metadata.get("caller_id") if metadata else None
        if not caller_id:
            logger.warning(
                "Draft-guard delivery deferred 5 times for terminal %s message(s) %s; "
                "no caller_id is available for notification",
                terminal_id,
                notify_ids,
            )
            return
        for message_id in notify_ids:
            try:
                create_inbox_message(
                    f"draft-guard:{terminal_id}",
                    caller_id,
                    f"[draft-guard] message {message_id} to terminal {terminal_id} has been "
                    "deferred 5 times because the composer state could not be confirmed; "
                    "delivery remains pending and will retry.",
                )
            except Exception:
                logger.warning(
                    "Failed to enqueue draft-guard notification for terminal %s message %s",
                    terminal_id,
                    message_id,
                    exc_info=True,
                )

    def _notify_delivery_failed(self, terminal_id: str, message_ids: list[int]) -> None:
        metadata = get_terminal_metadata(terminal_id)
        caller_id = metadata.get("caller_id") if metadata else None
        if not caller_id:
            logger.warning(
                "Delivery failed after 3 ambiguous attempts for terminal %s message(s) %s; "
                "no caller_id is available for notification", terminal_id, message_ids)
            return
        create_inbox_message(
            f"message-trace:{terminal_id}", caller_id,
            f"[message-trace] delivery to terminal {terminal_id} failed after 3 "
            f"ambiguous attempts for message(s) {message_ids}; inspect cao messages trace.",
        )

    def _commit_watchdog_ops(self, terminal_id: str, sender_id: str,
                             orchestration_type: OrchestrationType, metadata: dict) -> None:
        from cli_agent_orchestrator.services.stalled_callback_watchdog import stalled_callback_watchdog
        stalled_callback_watchdog.record_callback_if_to_caller(sender_id, terminal_id)
        if metadata.get("caller_id") and (
            orchestration_type == OrchestrationType.ASSIGN or
            (orchestration_type == OrchestrationType.SEND_MESSAGE and
             sender_id == metadata["caller_id"] and
             stalled_callback_watchdog.has_episode(terminal_id))
        ):
            stalled_callback_watchdog.record_inbound_task(
                terminal_id, metadata["caller_id"], metadata.get("agent_profile") or "")

    async def run(self, registry: PluginRegistry | None = None) -> None:
        queue = bus.subscribe("terminal.*.status")
        logger.info("InboxService started")

        while True:
            try:
                event = await queue.get()
                status_value = event["data"]["status"]
                if status_value in (TerminalStatus.IDLE.value, TerminalStatus.COMPLETED.value):
                    terminal_id = terminal_id_from_topic(event["topic"])
                    # deliver_pending does blocking DB + tmux I/O. Offload it to a
                    # worker thread so this consumer keeps yielding to the event loop
                    # (StatusMonitor/LogWriter must not be starved — see the threading
                    # note in docs/event-driven-architecture.md). The registry is
                    # threaded through so status-driven deliveries fire
                    # PostSendMessageEvent hooks with the same attribution as the
                    # immediate and OpenCode-poller paths.
                    await asyncio.to_thread(self.deliver_pending, terminal_id, registry=registry)
            except Exception as e:
                logger.error(f"Error in InboxService: {e}")

    def deliver_pending(
        self,
        terminal_id: str,
        num_messages: int = 1,
        registry: PluginRegistry | None = None,
    ) -> None:
        """Deliver pending message(s) to a ready terminal. Use num_messages=0 for all.

        Status comes from the StatusMonitor (the event-driven source of truth).
        Delivery normally happens on IDLE/COMPLETED; providers that accept input
        mid-turn (``accepts_input_while_processing``) also receive messages while
        PROCESSING/WAITING_USER_ANSWER when ``EAGER_INBOX_DELIVERY`` is on (#251).
        When a plugin registry is supplied, the originating sender and a
        ``send_message`` orchestration type are threaded to ``terminal_service``
        so ``PostSendMessageEvent`` hooks fire with correct attribution.
        """
        with _delivery_seq_guard:
            captured_wake = _delivery_wake_seq.get(terminal_id, 0)
        with _get_delivery_lock(terminal_id):
            with _delivery_seq_guard:
                if _delivery_wake_seq.get(terminal_id, 0) > captured_wake:
                    return
            limit = num_messages if num_messages > 0 else 100
            messages = get_pending_messages(terminal_id, limit=limit)
            if not messages:
                return

            provider = None
            if _should_defer_waiting(terminal_id):
                return
            status = status_monitor.get_status(terminal_id)
            if status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
                # Not ready on the normal path. Eager delivery (#251) lets providers
                # that accept input mid-turn receive messages while PROCESSING or
                # WAITING_USER_ANSWER; only in that case do we need the provider.
                eager_eligible = False
                if EAGER_INBOX_DELIVERY and status in (
                    TerminalStatus.PROCESSING,
                    TerminalStatus.WAITING_USER_ANSWER,
                ):
                    if provider is None:
                        provider = provider_manager.get_provider(terminal_id)
                    eager_eligible = provider is not None and getattr(
                        provider, "accepts_input_while_processing", False
                    )
                if not eager_eligible:
                    return

            # Deliver in contiguous runs of the same sender and orchestration mode.
            # With the default num_messages=1 this is a single run; when draining
            # all pending messages (num_messages=0) a batch can span multiple groups,
            # so each run is sent separately to keep attribution and shaping correct.
            sent_count = 0
            for (sender_id, orchestration_type), group in groupby(
                messages, key=lambda m: (m.sender_id, m.orchestration_type)
            ):
                batch = list(group)
                combined = "\n".join(m.message for m in batch)
                attempt_uuid = None
                try:
                    if _should_defer_waiting(terminal_id, provider):
                        _defer_messages(terminal_id, messages[sent_count:])
                        return
                    ambiguous_count = count_ambiguous_attempts([m.id for m in batch])
                    metadata = get_terminal_metadata(terminal_id) or {}
                    message_ids = [m.id for m in batch]
                    path = resolve_session_transcript(metadata)
                    if path is not None:
                        for prior in list_message_attempts(message_ids):
                            if prior.get("outcome") in {None, "deferred", "failed", "unresolved"}:
                                continue
                            try:
                                prior_evidence = json.loads(prior.get("evidence") or "{}")
                            except (TypeError, json.JSONDecodeError):
                                prior_evidence = {}
                            result, evidence = transcript_lookup(
                                path, prior["payload_hash"], prior.get("started_at"),
                                prior_evidence)
                            if result == "hit":
                                won = confirm_batch_from_prior_attempt(
                                    message_ids,
                                    prior["attempt_uuid"],
                                    on_confirmed=lambda: self._commit_watchdog_ops(
                                        terminal_id, sender_id, orchestration_type, metadata),
                                )
                                if not won:
                                    return
                                logger.info("Deduplicated delivery for terminal %s using attempt %s",
                                            terminal_id, prior["attempt_uuid"])
                                return
                            if result == "unresolved":
                                logger.warning(
                                    "Transcript continuity is uncertain for terminal %s; "
                                    "deferring retry without paste", terminal_id)
                                with _delivery_seq_guard:
                                    _delivery_wake_seq[terminal_id] = (
                                        _delivery_wake_seq.get(terminal_id, 0) + 1)
                                return
                    if ambiguous_count >= 3:
                        if transition_pending_to_delivery_failed(message_ids):
                            self._notify_delivery_failed(terminal_id, message_ids)
                        logger.warning("Delivery ambiguity cap reached for terminal %s messages %s",
                                       terminal_id, message_ids)
                        return
                    shape_type = (
                        None if registry is None and
                        orchestration_type == OrchestrationType.SEND_MESSAGE
                        else orchestration_type
                    )
                    prepared = terminal_service.prepare_input(terminal_id, combined, shape_type)
                    digest = wire_hash(prepared)
                    provider_name = metadata.get("provider", "unknown")
                    attempt_uuid = begin_delivery_attempt(
                        batch, terminal_id, provider_name, digest, len(prepared.encode()),
                        status_monitor.get_input_gen(terminal_id),
                        status_monitor.get_status_gen(terminal_id),
                        evidence=json.dumps(transcript_ref(path)),
                    )
                    terminal_service.send_prepared_input(
                        terminal_id, prepared, defer_on_dialog=True, registry=registry,
                        sender_id=sender_id, orchestration_type=shape_type,
                        original_message=combined)
                    trace = get_message_trace(batch[0].id)
                    current_attempt = next(x for x in trace["attempts"]
                                           if x["attempt_uuid"] == attempt_uuid)
                    outcome, evidence = confirm_delivery(
                        metadata, digest, current_attempt["started_at"],
                        current_attempt.get("evidence"))
                    if outcome in {"hit", "unverified"}:
                        settle_delivery_attempt(
                            attempt_uuid, MessageStatus.DELIVERED, "confirmed",
                            evidence=json.dumps(evidence),
                            settled_status_gen=status_monitor.get_status_gen(terminal_id),
                            on_confirmed=lambda: self._commit_watchdog_ops(
                                terminal_id, sender_id, orchestration_type, metadata),
                        )
                    else:
                        settle_delivery_attempt(attempt_uuid, MessageStatus.PENDING,
                                                "ambiguous", reason="confirmation_timeout",
                                                evidence=json.dumps(evidence),
                                                settled_status_gen=status_monitor.get_status_gen(terminal_id))
                        with _delivery_seq_guard:
                            _delivery_wake_seq[terminal_id] = _delivery_wake_seq.get(terminal_id, 0) + 1
                        return
                    logger.info(f"Delivered {len(batch)} message(s) to terminal {terminal_id}")
                    self._evict_defer_state(batch)
                except DeliveryDeferredError:
                    self._record_delivery_deferred(terminal_id, batch)
                    if attempt_uuid:
                        settle_delivery_attempt(attempt_uuid, MessageStatus.PENDING, "deferred",
                                                reason="delivery_deferred")
                    else:
                        _defer_messages(terminal_id, messages[sent_count:])
                    with _delivery_seq_guard:
                        _delivery_wake_seq[terminal_id] = _delivery_wake_seq.get(terminal_id, 0) + 1
                    return
                except TerminalInputBlockedError:
                    if attempt_uuid:
                        settle_delivery_attempt(attempt_uuid, MessageStatus.PENDING, "deferred",
                                                reason="input_blocked")
                    else: _defer_messages(terminal_id, messages[sent_count:])
                    with _delivery_seq_guard:
                        _delivery_wake_seq[terminal_id] = _delivery_wake_seq.get(terminal_id, 0) + 1
                    return
                except TerminalNotFoundError as e:
                    self._evict_defer_state(batch)
                    # Pane not resolvable yet (e.g. a herdr pane that isn't mapped
                    # for this window). Treat as transient: reset to PENDING so the
                    # reconcile sweep retries rather than marking FAILED. These were
                    # optimistically set to DELIVERED above. (#271 semantic.)
                    if attempt_uuid:
                        settle_delivery_attempt(attempt_uuid, MessageStatus.PENDING, "interrupted",
                                                reason="terminal_not_found", error=str(e))
                    else:
                        for message in batch: update_message_status(message.id, MessageStatus.PENDING)
                    logger.warning(
                        f"Pane not resolvable for terminal {terminal_id}; leaving "
                        f"{len(batch)} message(s) pending for retry: {e}"
                    )
                    with _delivery_seq_guard:
                        _delivery_wake_seq[terminal_id] = _delivery_wake_seq.get(terminal_id, 0) + 1
                except Exception as e:
                    self._evict_defer_state(batch)
                    if attempt_uuid:
                        settle_delivery_attempt(attempt_uuid, MessageStatus.FAILED, "failed",
                                                reason=type(e).__name__, error=str(e))
                    for message in batch:
                        logger.error(
                            f"Failed to deliver message {message.id} to {terminal_id}: {e}"
                        )
                        if not attempt_uuid: update_message_status(message.id, MessageStatus.FAILED)
                    with _delivery_seq_guard:
                        _delivery_wake_seq[terminal_id] = _delivery_wake_seq.get(terminal_id, 0) + 1
                sent_count += len(batch)

    def poll_opencode_pending_messages(self, registry: PluginRegistry | None = None) -> None:
        """Poll OpenCode terminals for pending inbox messages.

        OpenCode-specific wakeup path for providers whose pipe-pane logs do not
        change after the TUI settles, so the FIFO-driven StatusMonitor may not
        emit an IDLE/COMPLETED transition to trigger delivery on its own.
        """
        for terminal_id in list_pending_receiver_ids_by_provider(ProviderType.OPENCODE_CLI.value):
            try:
                self.deliver_pending(terminal_id, registry=registry)
            except Exception as e:
                logger.debug(f"OpenCode inbox poll failed for {terminal_id}: {e}")

    def reconcile_orphaned_messages(self, registry: PluginRegistry | None = None) -> None:
        """Re-attempt delivery for messages stuck in PENDING past the grace window.

        Provider-agnostic safety net for issue #131: when a receiving terminal is
        already idle, the immediate (on POST) delivery path may miss on a stale
        status, and an idle terminal produces no new output so the event-driven
        StatusMonitor never emits an IDLE/COMPLETED event to wake delivery —
        leaving the message orphaned. This sweep finds any such message and routes
        it back through the normal delivery gate (``deliver_pending``).

        Only messages older than ``INBOX_RECONCILE_GRACE_SECONDS`` are considered,
        so the sweep never competes with the fast paths for freshly queued
        messages — it only adopts ones they have already missed.
        """
        for terminal_id in list_pending_receiver_ids_older_than(INBOX_RECONCILE_GRACE_SECONDS):
            try:
                self.deliver_pending(terminal_id, registry=registry)
            except Exception as e:
                logger.debug(f"Inbox reconciliation failed for {terminal_id}: {e}")

    def recover_stale_deliveries(self) -> None:
        """Settle DELIVERING rows left by a process crash before consumers start."""
        seen_attempts: set[str] = set()
        for message in list_stale_delivering_messages():
            trace = get_message_trace(message.id)
            if not trace or not trace["attempts"]:
                update_message_status(message.id, MessageStatus.DELIVERY_FAILED)
                self._notify_delivery_failed(message.receiver_id, [message.id])
                continue
            attempt = trace["attempts"][-1]
            attempt_uuid = attempt["attempt_uuid"]
            if attempt_uuid in seen_attempts:
                continue
            seen_attempts.add(attempt_uuid)
            message_ids = list_attempt_member_ids(attempt_uuid) or [message.id]
            metadata = get_terminal_metadata(message.receiver_id)
            if not metadata:
                settle_delivery_attempt(attempt_uuid, MessageStatus.FAILED,
                                        "failed", reason="receiver_metadata_gone")
                continue
            try:
                from cli_agent_orchestrator.backends.registry import get_backend
                get_backend().get_history(metadata["tmux_session"], metadata["tmux_window"],
                                          tail_lines=1)
            except Exception:
                settle_delivery_attempt(attempt_uuid, MessageStatus.PENDING,
                                        "interrupted", reason="pane_unresolvable")
                continue
            path = resolve_session_transcript(metadata)
            if path is None:
                settle_delivery_attempt(attempt_uuid, MessageStatus.PENDING,
                                        "interrupted", reason="no_oracle")
                continue
            result, evidence = transcript_lookup(
                path, attempt["payload_hash"], attempt.get("started_at"),
                attempt.get("evidence"))
            if result == "hit":
                settle_delivery_attempt(
                    attempt_uuid, MessageStatus.DELIVERED, "confirmed",
                    reason="startup_sweep", evidence=json.dumps(evidence),
                    on_confirmed=lambda: self._commit_watchdog_ops(
                        message.receiver_id, attempt["sender_id"],
                        OrchestrationType(attempt["orchestration_type"]), metadata),
                )
            elif result == "absent":
                settle_delivery_attempt(attempt_uuid, MessageStatus.PENDING,
                                        "interrupted", reason="proven_absent",
                                        evidence=json.dumps(evidence))
            else:
                settle_delivery_attempt(attempt_uuid, MessageStatus.DELIVERY_FAILED,
                                        "unresolved", reason="continuity_uncertain",
                                        evidence=json.dumps(evidence))
                self._notify_delivery_failed(message.receiver_id, message_ids)


inbox_service = InboxService()
