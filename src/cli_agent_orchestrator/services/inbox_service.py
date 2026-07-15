"""Delivers queued inbox messages when terminals become ready.

Consumer: terminal.{id}.status
"""

import asyncio
import logging
import json
import threading
from datetime import datetime, timezone
from itertools import groupby

from cli_agent_orchestrator.backends.base import TerminalNotFoundError
from cli_agent_orchestrator.clients.database import (
    AdmissionProof,
    AttemptOpenResult,
    OrphanReconcileResult,
    begin_delivery_attempt,
    begin_delivery_attempt_if_no_other_delivering,
    confirm_batch_from_prior_attempt,
    count_ambiguous_attempts,
    create_inbox_message,
    get_terminal_metadata,
    get_pending_messages,
    get_pending_messages_by_ids,
    list_pending_receiver_ids_by_provider,
    list_pending_receiver_ids_older_than,
    update_message_status,
    settle_delivery_attempt,
    list_stale_delivering_messages,
    get_message_trace,
    list_attempt_member_ids,
    list_message_attempts,
    transition_pending_to_delivery_failed,
    merge_wpm1_attempt_evidence,
    record_wpm1_stalled_notice,
    settle_wpm1_terminal_batch,
    make_admission_proof,
    list_overlapping_attempts,
    list_delivering_attempts_for_terminal,
    list_stale_open_claude_attempts,
    recover_wpm2_stale_attempt,
    settle_delivery_attempt_proof_safe,
    settle_pending_orphan_messages,
    advance_wpm2_continuity_cursor,
    attempt_proven_pre_paste,
)

_PRODUCTION_BEGIN_DELIVERY_ATTEMPT = begin_delivery_attempt
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
    confirm_delivery, continuity_aware_lookup, resolve_session_transcript,
    transcript_lookup, transcript_ref, wire_hash,
    wpm2_lookup as _wpm2_lookup, wpm2_cursor_baseline,
)
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

IDLE_STALL_AGE = 30 * 60
ABS_STALLED_NOTICE_AGE = 4 * 60 * 60
WPM2_STALE_OPEN_AGE_SECONDS = 60
REDELIVERY_TAG_VERSION = 1


def _redelivery_tag(prior_attempt_uuid: str) -> str:
    return (
        f"[redelivery of attempt {prior_attempt_uuid[:8]} - prior delivery unconfirmed; "
        "ignore if already received]"
    )


def classify_permanently_d2_only(attempt: dict, current_observation_epoch: str | None) -> str:
    if attempt.get("outcome") != "ambiguous" or attempt.get("reason") != "confirmation_timeout":
        return "normal"
    try:
        evidence = json.loads(attempt.get("evidence") or "{}")
    except (TypeError, json.JSONDecodeError):
        return "anchor_missing"
    if not isinstance(evidence, dict):
        return "anchor_missing"
    if "busy_initial_submit" in evidence:
        return "busy_initial"
    anchor = evidence.get("injection_completed_seq")
    if not isinstance(anchor, dict):
        return "anchor_missing"
    epoch, seq = anchor.get("observation_epoch"), anchor.get("seq")
    if not isinstance(epoch, str) or not epoch or type(seq) is not int:
        return "anchor_missing"
    if current_observation_epoch is None:
        return "transient_snapshot_unavailable"
    return "epoch_mismatch" if epoch != current_observation_epoch else "normal"


_delivery_locks: dict[str, threading.Lock] = {}
_delivery_locks_guard = threading.Lock()
_delivery_wake_seq: dict[str, int] = {}
_delivery_seq_guard = threading.Lock()


def get_delivery_lock(terminal_id: str) -> threading.Lock:
    with _delivery_locks_guard:
        lock = _delivery_locks.get(terminal_id)
        if lock is None:
            lock = threading.Lock()
            _delivery_locks[terminal_id] = lock
        return lock


_get_delivery_lock = get_delivery_lock


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

    def _notify_delivery_failed(
        self, terminal_id: str, message_ids: list[int], reason: str = "confirmation_timeout"
    ) -> None:
        metadata = get_terminal_metadata(terminal_id)
        caller_id = metadata.get("caller_id") if metadata else None
        if not caller_id:
            logger.warning(
                "Delivery failed (%s) for terminal %s message(s) %s; no caller_id is "
                "available for notification", reason, terminal_id, message_ids)
            return
        if reason == "receiver_gone":
            body = (f"[message-trace] delivery to terminal {terminal_id} failed because the "
                    f"receiver terminal no longer exists for message(s) {message_ids}.")
        else:
            body = (f"[message-trace] delivery to terminal {terminal_id} failed after 3 "
                    f"ambiguous attempts for message(s) {message_ids}; inspect cao messages trace.")
        create_inbox_message(
            f"message-trace:{terminal_id}", caller_id, body,
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

    @staticmethod
    def _exact_batch_attempts(message_ids: list[int]) -> list[dict]:
        wanted = set(message_ids)
        exact: list[dict] = []
        seen: set[str] = set()
        for attempt in list_message_attempts(message_ids):
            attempt_uuid = attempt["attempt_uuid"]
            if attempt_uuid in seen:
                continue
            seen.add(attempt_uuid)
            if set(list_attempt_member_ids(attempt_uuid)) == wanted:
                exact.append(attempt)
        return exact

    def _handle_wpm1_gate(
        self, terminal_id: str, batch, metadata: dict, provider,
        sender_id: str, orchestration_type: OrchestrationType,
    ) -> tuple[str, object | None]:
        """Return normal, stop, or inject for a frozen-law gated batch."""
        message_ids = [message.id for message in batch]
        attempts = self._exact_batch_attempts(message_ids)
        ambiguous = [attempt for attempt in attempts if
                     attempt.get("outcome") == "ambiguous" and
                     attempt.get("reason") == "confirmation_timeout"]
        if not ambiguous:
            return "normal", None
        # D1.1 is deliberately before continuity/evidence decoding. Historical
        # malformed rows must not make a dead receiver look non-authoritative.
        if not metadata and any(item.get("provider") == "claude_code" for item in ambiguous):
            result = settle_wpm1_terminal_batch(
                message_ids, MessageStatus.DELIVERY_FAILED, terminal_id,
                reason="receiver_gone")
            if result == "settled":
                self._notify_delivery_failed(terminal_id, message_ids, reason="receiver_gone")
            return "stop", None
        decoded: dict[str, dict] = {}
        for attempt in ambiguous:
            try:
                value = json.loads(attempt.get("evidence") or "{}")
                decoded[attempt["attempt_uuid"]] = value if isinstance(value, dict) else {}
            except (TypeError, json.JSONDecodeError):
                decoded[attempt["attempt_uuid"]] = {}
        resolution = resolve_session_transcript(metadata) if metadata else None
        authoritative = (
            (metadata.get("provider") == "claude_code" or any(
                item.get("provider") == "claude_code" for item in ambiguous))
            and (getattr(resolution, "resolution_kind", None) == "binding" or any(
                value.get("resolution_kind") == "binding" for value in decoded.values()))
        )
        if not authoritative:
            return "normal", resolution

        newest = ambiguous[-1]
        now = datetime.now(timezone.utc)
        now_z = now.isoformat().replace("+00:00", "Z")

        lookup_result = "unresolved"
        for prior in reversed(ambiguous):
            prior_evidence = decoded[prior["attempt_uuid"]]
            lookup_result, lookup_evidence = _wpm2_lookup(
                metadata, prior["payload_hash"], prior.get("started_at"),
                prior_evidence,
            )
            if lookup_result == "hit":
                result = settle_wpm1_terminal_batch(
                    message_ids, MessageStatus.DELIVERED, terminal_id,
                    confirmation_evidence=(prior["attempt_uuid"], lookup_evidence),
                    on_confirmed=lambda: self._commit_watchdog_ops(
                        terminal_id, sender_id, orchestration_type, metadata))
                return "stop", None
            corroboration = lookup_evidence.get("queue_corroboration")
            if corroboration is not None:
                merge_wpm1_attempt_evidence(
                    prior["attempt_uuid"], message_ids,
                    {"queue_corroboration": corroboration})
            if lookup_result == "absent" and lookup_evidence.get("last_observed_ref"):
                _, expected = wpm2_cursor_baseline(prior_evidence)
                if expected is None:
                    return "stop", None
                advanced = advance_wpm2_continuity_cursor(
                    prior["attempt_uuid"], message_ids, expected,
                    lookup_evidence["last_observed_ref"])
                if advanced not in {"advanced", "already_advanced"}:
                    return "stop", None
                prior_evidence["last_observed_ref"] = lookup_evidence["last_observed_ref"]

        try:
            snapshot = status_monitor.get_boundary_observation(terminal_id)
            if (not isinstance(getattr(snapshot, "status", None), TerminalStatus) or
                    not isinstance(getattr(snapshot, "observation_epoch", None), str)):
                snapshot = None
        except Exception:
            snapshot = None
        status = (snapshot.status if snapshot is not None else
                  status_monitor.get_status(terminal_id))
        newest_evidence = decoded[newest["attempt_uuid"]]
        protection = classify_permanently_d2_only(
            newest, snapshot.observation_epoch if snapshot is not None else None)
        last_activity = newest_evidence.get("last_activity_at")
        updates: dict[str, object] = {
            "last_observed_status": status.value,
        }
        prior_status = newest_evidence.get("last_observed_status")
        if last_activity is None:
            settled = newest.get("settled_at")
            if isinstance(settled, datetime):
                if settled.tzinfo is None:
                    settled = settled.replace(tzinfo=timezone.utc)
                last_activity = settled.isoformat().replace("+00:00", "Z")
            else:
                last_activity = now_z
            updates["last_activity_at"] = last_activity
        elif snapshot is not None and prior_status != status.value:
            last_activity = now_z
            updates["last_activity_at"] = now_z
        if merge_wpm1_attempt_evidence(
                newest["attempt_uuid"], message_ids, updates) is not True:
            return "stop", None
        newest_evidence.update(updates)

        def parsed(value) -> datetime:
            if isinstance(value, datetime):
                result = value
            else:
                result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return result if result.tzinfo else result.replace(tzinfo=timezone.utc)

        try:
            activity_age = (now - parsed(last_activity)).total_seconds()
            newest_age = (now - parsed(newest.get("settled_at"))).total_seconds()
            notice_due = activity_age >= IDLE_STALL_AGE or newest_age >= ABS_STALLED_NOTICE_AGE
        except (TypeError, ValueError):
            notice_due = False

        gate_open = protection == "normal" and status in {
            TerminalStatus.IDLE, TerminalStatus.COMPLETED}
        if gate_open:
            if provider is None:
                provider = provider_manager.get_provider(terminal_id)
            gate_open = provider is not None and provider.read_composer_draft_state() == "empty"

        # A boundary requires an anchored same-epoch PROCESSING->ready cycle.
        if gate_open:
            anchor = newest_evidence.get("injection_completed_seq") or {}
            non_ready = snapshot.last_non_ready_seq if snapshot is not None else None
            ready = snapshot.last_ready_seq if snapshot is not None else None
            gate_open = (
                snapshot is not None
                and anchor.get("observation_epoch") == snapshot.observation_epoch
                and type(anchor.get("seq")) is int
                and type(non_ready) is int and non_ready > anchor["seq"]
                and type(ready) is int and ready > non_ready
            )
        if gate_open:
            fresh, fresh_evidence = _wpm2_lookup(
                metadata, newest["payload_hash"], newest.get("started_at"), newest_evidence)
            if fresh == "hit":
                result = settle_wpm1_terminal_batch(
                    message_ids, MessageStatus.DELIVERED, terminal_id,
                    confirmation_evidence=(newest["attempt_uuid"], fresh_evidence),
                    on_confirmed=lambda: self._commit_watchdog_ops(
                        terminal_id, sender_id, orchestration_type, metadata))
                return "stop", None
            if fresh == "absent":
                unexhausted = next((attempt for attempt in reversed(ambiguous)
                                    if not decoded[attempt["attempt_uuid"]].get(
                                        "boundary_exhausted_at")), None)
                if unexhausted is not None:
                    boundary_snapshot = {
                        "observation_epoch": (snapshot.observation_epoch if snapshot else "legacy"),
                        "status": status.value,
                        "status_gen": (snapshot.status_gen if snapshot else
                                       status_monitor.get_status_gen(terminal_id)),
                        "input_gen": (snapshot.input_gen if snapshot else
                                      status_monitor.get_input_gen(terminal_id)),
                        "seq": (snapshot.seq if snapshot else 0),
                        "last_non_ready_seq": (snapshot.last_non_ready_seq if snapshot else None),
                        "last_ready_seq": (snapshot.last_ready_seq if snapshot else None),
                    }
                    if merge_wpm1_attempt_evidence(
                        unexhausted["attempt_uuid"], message_ids,
                        {"boundary_exhausted_at": now_z,
                         "boundary_snapshot": boundary_snapshot},
                    ) is not True:
                        return "stop", None
                    decoded[unexhausted["attempt_uuid"]]["boundary_exhausted_at"] = now_z
                exhausted = sum(bool(decoded[item["attempt_uuid"]].get(
                    "boundary_exhausted_at")) for item in ambiguous)
                if exhausted >= 3:
                    barrier, _ = _wpm2_lookup(
                        metadata, newest["payload_hash"], newest.get("started_at"),
                        decoded[newest["attempt_uuid"]])
                    if barrier == "hit":
                        result = settle_wpm1_terminal_batch(
                            message_ids, MessageStatus.DELIVERED, terminal_id,
                            on_confirmed=lambda: self._commit_watchdog_ops(
                                terminal_id, sender_id, orchestration_type, metadata))
                    elif barrier == "absent":
                        result = settle_wpm1_terminal_batch(
                            message_ids, MessageStatus.DELIVERY_FAILED, terminal_id)
                        if result == "settled":
                            self._notify_delivery_failed(terminal_id, message_ids)
                    return "stop", None
                successors = [
                    item for item in attempts
                    if item.get("prior_attempt_uuid") == newest["attempt_uuid"]
                ]
                if successors:
                    if all(attempt_proven_pre_paste(item) for item in successors):
                        return "normal", None
                    return "stop", None
                evidence = transcript_ref(resolution)
                evidence["boundary_authorized"] = now_z
                evidence["_wpm1_prior_attempt_uuid"] = newest["attempt_uuid"]
                return "inject", evidence

        # Threshold decisions are deliberately after every proof/terminal arm.
        is_notice = any(
            str(message.sender_id).startswith("message-trace:") and
            str(message.message).startswith("wpm1-notice ") for message in batch)
        already_notified = any(decoded[item["attempt_uuid"]].get("stalled_notified_at")
                               for item in ambiguous)
        if notice_due and not already_notified and not is_notice:
            outcome = record_wpm1_stalled_notice(
                newest["attempt_uuid"], message_ids, terminal_id, now_z)
            if outcome == "busy_aborted":
                return "stop", None
        if protection != "normal":
            return "skip_d2_only", {
                "attempt_uuid": newest["attempt_uuid"], "member_ids": message_ids,
                "protection_reason": protection,
            }
        return "stop", None

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
        delivery_lock = get_delivery_lock(terminal_id)
        if not delivery_lock.acquire(blocking=False):
            # Rebind owns the exclusion lock. Keep every message PENDING and
            # advance the wake generation so the next ready event retries.
            with _delivery_seq_guard:
                _delivery_wake_seq[terminal_id] = _delivery_wake_seq.get(terminal_id, 0) + 1
            return
        try:
            metadata = get_terminal_metadata(terminal_id) or {}
            if metadata.get("recovery_state") not in (None, "rebound"):
                return
            with _delivery_seq_guard:
                if _delivery_wake_seq.get(terminal_id, 0) > captured_wake:
                    return
            limit = num_messages if num_messages > 0 else 100
            provider = None
            excluded: set[int] = set()
            scanned: set[int] = set()
            legacy_test_seam = begin_delivery_attempt is not _PRODUCTION_BEGIN_DELIVERY_ATTEMPT
            # Classify protected sets before the SQL LIMIT/grouping seam. This
            # deliberately scans beyond any number of D2-only heads.
            while not legacy_test_seam:
                page = get_pending_messages(
                    terminal_id, limit=100, excluded_message_ids=excluded | scanned)
                if not page:
                    break
                first = page[0]
                if first.id in excluded or first.id in scanned:
                    break
                first_attempts = list_message_attempts([first.id])
                protected_attempt = next((item for item in reversed(first_attempts)
                    if item.get("outcome") == "ambiguous" and
                    item.get("reason") == "confirmation_timeout"), None)
                if protected_attempt is not None:
                    durable_ids = list_attempt_member_ids(protected_attempt["attempt_uuid"])
                    group = get_pending_messages_by_ids(terminal_id, durable_ids)
                else:
                    _, first_group = next(groupby(
                        page, key=lambda item: (item.sender_id, item.orchestration_type)))
                    group = list(first_group)
                if not group:
                    scanned.add(first.id)
                    continue
                state, detail = self._handle_wpm1_gate(
                    terminal_id, group, metadata, provider,
                    first.sender_id, first.orchestration_type)
                ids = {item.id for item in group}
                if state == "skip_d2_only":
                    member_ids = set((detail or {}).get("member_ids") or ids)
                    excluded.update(member_ids)
                    scanned.difference_update(member_ids)
                    continue
                if state == "stop":
                    return
                scanned.update(ids)
            if legacy_test_seam:
                messages = get_pending_messages(terminal_id, limit=limit)
            else:
                messages = get_pending_messages(
                    terminal_id, limit=limit, excluded_message_ids=excluded)
            if not messages:
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
                submit_observation = None
                submit_evidence = None
                try:
                    metadata = get_terminal_metadata(terminal_id) or {}
                    message_ids = [m.id for m in batch]
                    gate_state, gate_evidence = self._handle_wpm1_gate(
                        terminal_id, batch, metadata, provider, sender_id, orchestration_type)
                    if gate_state == "stop":
                        return
                    if gate_state == "skip_d2_only":
                        continue
                    admission_snapshot = None
                    admission_kind = "corrective" if gate_state == "inject" else "ordinary"
                    if gate_state == "normal":
                        if _should_defer_waiting(terminal_id, provider):
                            return
                        if not legacy_test_seam:
                            try:
                                admission_snapshot = status_monitor.get_boundary_observation(terminal_id)
                            except Exception:
                                return
                        if not isinstance(getattr(admission_snapshot, "status", None), TerminalStatus):
                            admission_snapshot = None
                            status = status_monitor.get_status(terminal_id)
                            if (metadata.get("provider") == "claude_code" and status not in {
                                    TerminalStatus.IDLE, TerminalStatus.COMPLETED}):
                                return
                        else:
                            status = admission_snapshot.status
                        if (metadata.get("provider") == "claude_code" and status not in {
                                TerminalStatus.IDLE, TerminalStatus.COMPLETED}):
                            overlap = list_overlapping_attempts(message_ids)
                            if all(item.get("outcome") == "deferred" and item.get("reason") in {
                                    "delivery_deferred", "input_blocked"} for item in overlap):
                                if provider is None:
                                    provider = provider_manager.get_provider(terminal_id)
                                if provider is not None:
                                    if provider.read_composer_draft_state() != "empty":
                                        return
                                    admission_kind = "s4_initial"
                        if status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
                            eager_eligible = False
                            if metadata.get("provider") == "claude_code":
                                if provider is None:
                                    provider = provider_manager.get_provider(terminal_id)
                                eager_eligible = admission_kind == "s4_initial"
                            elif EAGER_INBOX_DELIVERY and status in (
                                TerminalStatus.PROCESSING,
                                TerminalStatus.WAITING_USER_ANSWER,
                            ):
                                if provider is None:
                                    provider = provider_manager.get_provider(terminal_id)
                                eager_eligible = provider is not None and getattr(
                                    provider, "accepts_input_while_processing", False)
                            if not eager_eligible:
                                return
                    ambiguous_count = count_ambiguous_attempts(message_ids)
                    resolution = resolve_session_transcript(metadata)
                    if gate_state == "normal" and resolution is not None:
                        for prior in list_message_attempts(message_ids):
                            if prior.get("outcome") in {None, "deferred", "failed", "unresolved"}:
                                continue
                            try:
                                prior_evidence = json.loads(prior.get("evidence") or "{}")
                            except (TypeError, json.JSONDecodeError):
                                prior_evidence = {}
                            result, evidence = _wpm2_lookup(
                                metadata, prior["payload_hash"], prior.get("started_at"),
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
                    if gate_state == "normal" and ambiguous_count >= 3:
                        if transition_pending_to_delivery_failed(message_ids):
                            self._notify_delivery_failed(terminal_id, message_ids)
                        logger.warning("Delivery ambiguity cap reached for terminal %s messages %s",
                                       terminal_id, message_ids)
                        return
                    if gate_state == "normal" and _should_defer_waiting(terminal_id, provider):
                        _defer_messages(terminal_id, messages[sent_count:])
                        return
                    successor_source = None
                    persisted_evidence = gate_evidence
                    if gate_state == "inject":
                        persisted_evidence = dict(gate_evidence or {})
                        successor_source = persisted_evidence.pop(
                            "_wpm1_prior_attempt_uuid", None)
                        if successor_source is not None:
                            persisted_evidence["redelivery_tag"] = {
                                "version": REDELIVERY_TAG_VERSION,
                                "prior_attempt_uuid": successor_source,
                            }
                    shape_type = (
                        None if registry is None and
                        orchestration_type == OrchestrationType.SEND_MESSAGE
                        else orchestration_type
                    )
                    base_prepared = terminal_service.prepare_input(
                        terminal_id, combined, shape_type)
                    wire_prepared = (
                        f"{_redelivery_tag(successor_source)}\n{base_prepared}"
                        if successor_source is not None else base_prepared
                    )
                    digest = wire_hash(wire_prepared)
                    provider_name = metadata.get("provider", "unknown")
                    proof = make_admission_proof(
                        admission_kind, message_ids, successor_source)
                    if not legacy_test_seam and list_delivering_attempts_for_terminal(terminal_id):
                        return
                    opener_args = (
                        batch, terminal_id, provider_name, digest,
                        len(wire_prepared.encode()),
                        status_monitor.get_input_gen(terminal_id),
                        status_monitor.get_status_gen(terminal_id),
                    )
                    opener_kwargs = {
                        "evidence": json.dumps(
                            persisted_evidence if gate_state == "inject" else transcript_ref(resolution)),
                        "prior_attempt_uuid": successor_source,
                    }

                    def evidence_at_submit(value):
                        if (not isinstance(getattr(value, "status", None), TerminalStatus)
                                or not isinstance(getattr(value, "observation_epoch", None), str)
                                or type(getattr(value, "seq", None)) is not int):
                            return None
                        result = dict(persisted_evidence or transcript_ref(resolution))
                        same_epoch = (admission_kind != "s4_initial" or (
                            admission_snapshot is not None and
                            admission_snapshot.observation_epoch == value.observation_epoch))
                        if not same_epoch:
                            return result
                        result["injection_completed_seq"] = {
                            "observation_epoch": value.observation_epoch, "seq": value.seq}
                        if admission_snapshot is not None and (
                                admission_snapshot.status not in {
                                    TerminalStatus.IDLE, TerminalStatus.COMPLETED}
                                or value.status not in {
                                    TerminalStatus.IDLE, TerminalStatus.COMPLETED}):
                            result["busy_initial_submit"] = {
                                "status_at_admission": admission_snapshot.status.value,
                                "status_at_submit": value.status.value,
                                "observation_epoch": value.observation_epoch,
                                "seq": value.seq,
                            }
                        return result
                    # Preserve the long-standing injectable test seam. Runtime
                    # delivery always uses the WPM2 atomic opener.
                    if legacy_test_seam:
                        opened = AttemptOpenResult.opened(
                            begin_delivery_attempt(*opener_args, **opener_kwargs))
                    else:
                        opened = begin_delivery_attempt_if_no_other_delivering(
                            *opener_args, admission_proof=proof, **opener_kwargs)
                    if opened.kind != "opened":
                        logger.debug("WPM2 opener held %s: %s", terminal_id, opened.kind)
                        return
                    attempt_uuid = opened.attempt_uuid
                    def submitted(value):
                        nonlocal submit_observation, submit_evidence
                        submit_observation = value
                        submit_evidence = evidence_at_submit(value)
                    try:
                        send_kwargs = {
                            "defer_on_dialog": True, "registry": registry,
                            "sender_id": sender_id, "orchestration_type": shape_type,
                            "original_message": combined,
                        }
                        if not legacy_test_seam:
                            send_kwargs["on_submitted"] = submitted
                        submit_observation = terminal_service.send_prepared_input(
                            terminal_id, wire_prepared, **send_kwargs)
                        if (not isinstance(getattr(submit_observation, "status", None), TerminalStatus)
                                or not isinstance(getattr(
                                    submit_observation, "observation_epoch", None), str)
                                or type(getattr(submit_observation, "seq", None)) is not int):
                            submit_observation = None
                            submit_evidence = None
                        else:
                            submit_evidence = evidence_at_submit(submit_observation)
                    except (DeliveryDeferredError, TerminalInputBlockedError):
                        if submit_observation is None:
                            raise
                        settle_delivery_attempt_proof_safe(
                            attempt_uuid, submit_evidence or {},
                            status_monitor.get_status_gen(terminal_id))
                        return
                    except Exception:
                        if submit_observation is None and legacy_test_seam:
                            raise
                        settle_delivery_attempt_proof_safe(
                            attempt_uuid, submit_evidence or dict(
                                persisted_evidence or transcript_ref(resolution)),
                            status_monitor.get_status_gen(terminal_id))
                        return
                    trace = get_message_trace(batch[0].id)
                    current_attempt = next(x for x in trace["attempts"]
                                           if x["attempt_uuid"] == attempt_uuid)
                    outcome, evidence = confirm_delivery(
                        metadata, digest, current_attempt["started_at"],
                        current_attempt.get("evidence"))
                    if gate_state == "inject":
                        evidence = {**current_attempt.get("evidence", {}), **evidence}
                    if submit_evidence is not None:
                        evidence.update(submit_evidence)
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
                        if submit_evidence is not None:
                            settle_delivery_attempt_proof_safe(
                                attempt_uuid, submit_evidence,
                                status_monitor.get_status_gen(terminal_id))
                            return
                        else:
                            settle_delivery_attempt(attempt_uuid, MessageStatus.PENDING, "deferred",
                                                    reason="delivery_deferred")
                    else:
                        _defer_messages(terminal_id, messages[sent_count:])
                    with _delivery_seq_guard:
                        _delivery_wake_seq[terminal_id] = _delivery_wake_seq.get(terminal_id, 0) + 1
                    return
                except TerminalInputBlockedError:
                    if attempt_uuid:
                        if submit_evidence is not None:
                            settle_delivery_attempt_proof_safe(
                                attempt_uuid, submit_evidence,
                                status_monitor.get_status_gen(terminal_id))
                            return
                        else:
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
                        if submit_evidence is not None:
                            settle_delivery_attempt_proof_safe(
                                attempt_uuid, submit_evidence,
                                status_monitor.get_status_gen(terminal_id))
                            return
                        else:
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
                        if submit_observation is None:
                            settle_delivery_attempt(
                                attempt_uuid, MessageStatus.FAILED, "failed", error=str(e))
                        else:
                            result = settle_delivery_attempt_proof_safe(
                                attempt_uuid, submit_evidence or {},
                                status_monitor.get_status_gen(terminal_id))
                            return
                    for message in batch:
                        logger.error(
                            f"Failed to deliver message {message.id} to {terminal_id}: {e}"
                        )
                        if not attempt_uuid: update_message_status(message.id, MessageStatus.FAILED)
                    with _delivery_seq_guard:
                        _delivery_wake_seq[terminal_id] = _delivery_wake_seq.get(terminal_id, 0) + 1
                    sent_count += len(batch)
        finally:
            delivery_lock.release()

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
        self.reconcile_pending_orphans()
        for terminal_id in list_pending_receiver_ids_older_than(INBOX_RECONCILE_GRACE_SECONDS):
            try:
                self.deliver_pending(terminal_id, registry=registry)
            except Exception as e:
                logger.debug(f"Inbox reconciliation failed for {terminal_id}: {e}")
        self.recover_stale_deliveries(recurring=True)

    def reconcile_pending_orphans(self) -> OrphanReconcileResult:
        """Settle one bounded batch of PENDING rows with absent receivers."""
        result = settle_pending_orphan_messages()
        if result.busy_aborted:
            logger.warning("P5 orphan reconciliation aborted after bounded database contention")
        elif result.settled_count:
            logger.info(
                "P5 orphan reconciliation settled %d message(s), queued %d notice(s), "
                "logged-only %d batch(es)",
                result.settled_count, result.notification_count, result.logged_only_count,
            )
        return result

    def _recover_wpm2_attempt(self, attempt: dict) -> None:
        terminal_id = attempt["receiver_terminal_id"]
        attempt_uuid = attempt["attempt_uuid"]
        message_ids = list(attempt.get("message_ids") or list_attempt_member_ids(attempt_uuid))
        lock = get_delivery_lock(terminal_id)
        acquired = lock.acquire(blocking=False)
        if not acquired:
            return
        try:
            metadata = get_terminal_metadata(terminal_id)
            if not metadata:
                recover_wpm2_stale_attempt(
                    attempt_uuid, message_ids, MessageStatus.DELIVERY_FAILED,
                    "failed", "receiver_gone", {})
                return
            try:
                evidence = json.loads(attempt.get("evidence") or "{}")
                if not isinstance(evidence, dict):
                    evidence = {}
            except (TypeError, json.JSONDecodeError):
                evidence = {}
            resolution = resolve_session_transcript(metadata)
            if resolution is None:
                lookup, lookup_evidence = "unresolved", {"kind": "transcript_unresolved"}
            else:
                lookup, lookup_evidence = _wpm2_lookup(
                    metadata, attempt["payload_hash"], attempt.get("started_at"), evidence)
            if lookup == "hit":
                result = recover_wpm2_stale_attempt(
                    attempt_uuid, message_ids, MessageStatus.DELIVERED,
                    "confirmed", "stale_recovery", lookup_evidence)
                if result == "settled":
                    self._commit_watchdog_ops(
                        terminal_id, attempt["sender_id"],
                        OrchestrationType(attempt["orchestration_type"]), metadata)
                return
            recovered_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            recovery_evidence = {
                **lookup_evidence,
                "crash_recovery": {
                    "kind": "possibly_submitted_without_anchor",
                    "recovered_at": recovered_at,
                    "lookup_kind": lookup_evidence.get("kind", "transcript_unresolved"),
                },
            }
            recover_wpm2_stale_attempt(
                attempt_uuid, message_ids, MessageStatus.PENDING,
                "ambiguous", "confirmation_timeout", recovery_evidence)
        finally:
            lock.release()

    def recover_stale_deliveries(self, recurring: bool = False) -> None:
        """Settle DELIVERING rows left by a process crash before consumers start."""
        if recurring:
            for attempt in list_stale_open_claude_attempts(WPM2_STALE_OPEN_AGE_SECONDS):
                self._recover_wpm2_attempt(attempt)
            return
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
            if attempt.get("provider") == "claude_code":
                self._recover_wpm2_attempt({
                    **attempt, "receiver_terminal_id": message.receiver_id,
                    "message_ids": message_ids,
                })
                continue
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
            resolution = resolve_session_transcript(metadata)
            if resolution is None:
                settle_delivery_attempt(attempt_uuid, MessageStatus.PENDING,
                                        "interrupted", reason="no_oracle")
                continue
            path = getattr(resolution, "path", resolution)
            result, evidence = transcript_lookup(
                path, attempt["payload_hash"], attempt.get("started_at"),
                attempt.get("evidence"))
            evidence["resolution_kind"] = getattr(resolution, "resolution_kind", "exact_id")
            stale_note = getattr(resolution, "stale_note", None)
            if stale_note:
                evidence["binding_stale"] = stale_note
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
