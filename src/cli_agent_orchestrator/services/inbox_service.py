"""Delivers queued inbox messages when terminals become ready.

Consumer: terminal.{id}.status
"""

import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from sqlalchemy.exc import OperationalError
from tzlocal import get_localzone

from cli_agent_orchestrator.backends.base import TerminalNotFoundError
from cli_agent_orchestrator.clients.database import (
    AdmissionProof,
    AttemptOpenResult,
    NoticeInsertOutcome,
    OrphanReconcileResult,
    _utcnow,
    advance_wpm2_continuity_cursor,
    attempt_proven_pre_paste,
    begin_delivery_attempt,
    begin_delivery_attempt_if_no_other_delivering,
    confirm_batch_from_prior_attempt,
    count_ambiguous_attempts,
    create_inbox_message,
    find_inferred_delivery_evidence,
    get_attempt_mailbox_authority,
    get_current_mailbox_terminal,
    get_latest_compact_transcript_binding,
    get_message_trace,
    get_owned_legacy_parked_messages,
    get_park_warm_for_message_ids,
    get_pending_messages,
    get_pending_messages_by_ids,
    get_terminal_metadata,
    insert_identity_authority_notice,
    list_attempt_member_ids,
    list_delivering_attempts_for_terminal,
    list_message_attempts,
    list_overlapping_attempts,
    list_pending_receiver_ids_by_provider,
    list_pending_receiver_ids_older_than,
    list_pending_receiver_ids_with_terminal,
    list_stale_delivering_messages,
    list_stale_open_claude_attempts,
    make_admission_proof,
    merge_wpm1_attempt_evidence,
    record_wpm1_stalled_notice,
    recover_transcript_binding_if_current,
    recover_wpm2_stale_attempt,
    settle_attempt_inferred_delivered_batch,
    settle_delivery_attempt,
    settle_delivery_attempt_proof_safe,
    settle_open_attempt_inferred_delivered,
    settle_pending_orphan_messages,
    settle_pending_receiver_gone_if_generation,
    settle_wpm1_terminal_batch,
    transition_pending_to_delivery_failed,
    transition_pending_to_inferred_delivered,
    update_message_status,
)

_PRODUCTION_BEGIN_DELIVERY_ATTEMPT = begin_delivery_attempt
from cli_agent_orchestrator.constants import (
    EAGER_INBOX_DELIVERY,
    INBOX_RECONCILE_GRACE_SECONDS,
)
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import receiver_state_view, settings_service, terminal_service
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.message_trace_service import (
    binding_presumed_stale,
    clear_binding_staleness_state,
    confirm_delivery,
    continuity_aware_lookup,
    normalized_confirmation_fingerprint,
    observe_binding_absence,
    resolve_session_transcript,
    scan_binding_candidates,
    transcript_lookup,
    transcript_ref,
    wire_hash,
    wpm2_cursor_baseline,
)
from cli_agent_orchestrator.services.message_trace_service import wpm2_lookup as _wpm2_lookup
from cli_agent_orchestrator.services.pane_identity_service import PaneIdentityMismatchError
from cli_agent_orchestrator.services.replay_policy import (
    AuthorizationFacts,
    ObservedFact,
    run_post_auth_engine,
)
from cli_agent_orchestrator.services.status_monitor import ScreenProbeMeta, status_monitor
from cli_agent_orchestrator.services.terminal_service import TerminalInputBlockedError
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

# F162 D10: rate-limited gate5 WARN state — {terminal_id: last_warn_time}
_fx158_gate5_last_warn: dict[str, float] = {}
_FX158_GATE5_WARN_INTERVAL_S: float = 60.0

IDLE_STALL_AGE = 30 * 60
ABS_STALLED_NOTICE_AGE = 4 * 60 * 60
WPM2_STALE_OPEN_AGE_SECONDS = 60
# After this many presumed-stale suppress cycles, stop blocking delivery and
# allow the normal confirm path (often send_returned_unverified). Matches the
# binding-authority notice threshold so the operator is told at the same time
# the deadlock is broken.
BINDING_SUPPRESS_MAX_CYCLES = 3

# F339: Maximum consecutive terminal-not-found (404) responses before an episode
# is permanently abandoned. Prevents unbounded CPU/request storms when terminal
# rows are wiped but in-memory delivery episodes persist.
_F339_TERMINAL_NOT_FOUND_MAX = 5


@dataclass(frozen=True)
class FirstLookupResult:
    kind: str
    evidence: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class SuccessorLookupPlan:
    attempt_uuid: str
    payload_hash: str
    started_at: object
    evidence_at_first_lookup: dict[str, Any]
    first_result: FirstLookupResult
    first_ref: tuple[str, int | None, int] | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "evidence_at_first_lookup", copy.deepcopy(self.evidence_at_first_lookup)
        )
        object.__setattr__(
            self,
            "first_result",
            FirstLookupResult(
                self.first_result.kind,
                copy.deepcopy(self.first_result.evidence),
                copy.deepcopy(self.first_result.metadata),
            ),
        )


@dataclass(frozen=True)
class SuccessorCorroborationResult:
    kind: Literal["confirmed", "defer", "authorize"]
    hit_attempt_uuid: str | None = None
    hit_evidence: dict[str, Any] | None = None


InjectSafetyReason = Literal[
    "waiting_status",
    "waiting_gate",
    "dialog_hazard",
    "identity_unverified",
    "safety_unverified",
]


@dataclass(frozen=True)
class InjectSafetyResult:
    verdict: Literal["safe", "veto"]
    reason: InjectSafetyReason | None = None
    gate_episode: str | None = None

    def __post_init__(self) -> None:
        if self.verdict == "safe" and (self.reason is not None or self.gate_episode is not None):
            raise ValueError("safe injection result cannot carry veto detail")
        if self.verdict == "veto" and self.reason is None:
            raise ValueError("veto injection result requires a closed reason")
        if self.reason != "waiting_gate" and self.gate_episode is not None:
            raise ValueError("only waiting-gate vetoes carry a gate episode")


def _lookup_ref(evidence: dict[str, Any]) -> tuple[str, int | None, int] | None:
    candidate = evidence.get("last_observed_ref")
    if not isinstance(candidate, dict):
        candidate = evidence
    path = candidate.get("path")
    inode = candidate.get("inode")
    size = candidate.get("size")
    if (
        not isinstance(path, str)
        or not path
        or (inode is not None and type(inode) is not int)
        or type(size) is not int
    ):
        return None
    return path, inode, size


def corroborate_claude_successor(
    plans: tuple[SuccessorLookupPlan, ...],
) -> SuccessorCorroborationResult:
    """Run the single read-only final corroboration pass for a Claude successor."""
    time.sleep(2.0)
    if not plans:
        return SuccessorCorroborationResult("defer")
    observed: list[tuple[SuccessorLookupPlan, str, dict[str, Any]]] = []
    for plan in plans:
        result, evidence = _wpm2_lookup(
            dict(plan.first_result.metadata),
            plan.payload_hash,
            plan.started_at,
            copy.deepcopy(plan.evidence_at_first_lookup),
        )
        observed.append((plan, result, evidence))
        if result == "hit":
            return SuccessorCorroborationResult(
                "confirmed", plan.attempt_uuid, copy.deepcopy(evidence)
            )
    for plan, result, evidence in observed:
        if result != "absent" or plan.first_ref is None:
            return SuccessorCorroborationResult("defer")
        if _lookup_ref(evidence) != plan.first_ref:
            return SuccessorCorroborationResult("defer")
    return SuccessorCorroborationResult("authorize")


def _successor_lookup_plan(
    attempt: dict[str, Any],
    evidence_snapshot: dict[str, Any],
    result: str,
    lookup_evidence: dict[str, Any],
    metadata: dict[str, Any],
) -> SuccessorLookupPlan:
    return SuccessorLookupPlan(
        attempt_uuid=attempt["attempt_uuid"],
        payload_hash=attempt["payload_hash"],
        started_at=attempt.get("started_at"),
        evidence_at_first_lookup=copy.deepcopy(evidence_snapshot),
        first_result=FirstLookupResult(
            result, copy.deepcopy(lookup_evidence), copy.deepcopy(metadata)
        ),
        first_ref=_lookup_ref(lookup_evidence),
    )


def _confirmed_settlement(operation: Callable[[], Any]) -> Any:
    from cli_agent_orchestrator.services.stalled_callback_watchdog import (
        stalled_callback_watchdog,
    )

    with stalled_callback_watchdog.confirmed_settlement_guard():
        return operation()


def _redelivery_tag(prior_attempt_uuid: str) -> str:
    return (
        f"[redelivery of attempt {prior_attempt_uuid[:8]} - prior delivery unconfirmed; "
        "ignore if already received]"
    )


def _wire_with_attempt_challenge(
    wire: str,
    sender_id: str,
    message_id: int,
) -> tuple[str, str | None]:
    """Splice a wire-only singleton challenge into the last authentic wrapper suffix."""
    # F172: the MCP injection now uses "[Message from <display_name> (<id>). ..."
    # but legacy messages use "[Message from terminal <id>. ...". Match both.
    from cli_agent_orchestrator.utils.terminal import display_name as _dn

    sender_dn = _dn(sender_id)
    # New form: [Message from <display_name> (<id>). ...]
    new_prefix = f"[Message from {sender_dn} ({sender_id}). "
    new_suffix = new_prefix + (
        "Use the cao-mcp-server send_message MCP tool for any follow-up work — "
        "never a built-in collaboration.send_message.]"
    )
    # Legacy form: [Message from terminal <id>. ...]
    legacy_prefix = f"[Message from terminal {sender_id}. "
    legacy_suffix = legacy_prefix + (
        "Use the cao-mcp-server send_message MCP tool for any follow-up work — "
        "never a built-in collaboration.send_message.]"
    )

    if wire.endswith(new_suffix):
        prefix = new_prefix
        suffix = new_suffix
    elif wire.endswith(legacy_suffix):
        prefix = legacy_prefix
        suffix = legacy_suffix
    else:
        return wire, None
    index = len(wire) - len(suffix)
    raw_challenge = secrets.token_hex(16)
    replacement = f"{prefix[:-2]} | mid {message_id}:{raw_challenge}. "
    challenged = wire[:index] + replacement + suffix[len(prefix) :]
    return challenged, hashlib.sha256(raw_challenge.encode()).hexdigest()


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


# WP-F44 required `receiver_status_at_settle == "processing"`. That came from
# the fixture the WP was built on -- a BUSY grok_tester, still mid-turn at settle
# -- and encodes the fixture's shape rather than a safety requirement.
#
# Measured 2026-07-27: of five ambiguous attempts in 24h, THREE had a stable
# inode and real transcript growth (+813, +1058, +8969 bytes) and were refused
# on status alone, because the receiver had reached COMPLETED. One of them also
# carried queue corroboration. They were redelivered; the worker saw each brief
# twice.
#
# `COMPLETED` means the receiver FINISHED a turn. For "did the payload land",
# a turn that started and finished while the transcript grew is not weaker
# evidence than one still running -- the growth is the same evidence, observed
# slightly later. The original gate excluded its own best case.
#
# What is deliberately NOT admitted: IDLE and UNKNOWN. Growth on an idle
# receiver is not attributable to this payload, and UNKNOWN is the sampler's
# own failure -- WP-F44's ruling that "unknown -> False" stands, and is the
# reason this is a closed set rather than a `!= IDLE` negation.
_EXECUTING_AT_SETTLE = frozenset({TerminalStatus.PROCESSING.value, TerminalStatus.COMPLETED.value})


def is_probable_delivered(evidence: dict) -> bool:
    """Classify execution evidence without filesystem or service dependencies."""
    if not isinstance(evidence, dict):
        return False
    if evidence.get("receiver_status_at_settle") not in _EXECUTING_AT_SETTLE:
        return False
    growth = evidence.get("transcript_growth")
    if not isinstance(growth, dict) or growth.get("inode_stable") is not True:
        return False
    size_at_open = growth.get("size_at_open")
    size_at_settle = growth.get("size_at_settle")
    if type(size_at_open) is not int or type(size_at_settle) is not int:
        return False
    if size_at_settle <= size_at_open:
        return False
    if "busy_initial_submit" not in evidence:
        return True
    busy = evidence.get("busy_initial_submit")
    return (
        isinstance(busy, dict) and busy.get("status_at_submit") == TerminalStatus.PROCESSING.value
    )


def _opener_transcript_ref(evidence: dict[str, Any]) -> dict[str, Any] | None:
    reference = _lookup_ref(evidence)
    if reference is None:
        return None
    path, inode, size = reference
    return {"path": path, "inode": inode, "size": size}


def _classify_probable_delivery(
    evidence: dict[str, Any],
    terminal_id: str,
    opener_ref: dict[str, Any] | None,
) -> dict[str, Any]:
    classified = dict(evidence) if isinstance(evidence, dict) else {}
    try:
        receiver_status = status_monitor.get_status(terminal_id)
    except Exception:
        receiver_status = None
    classified["receiver_status_at_settle"] = (
        receiver_status.value if isinstance(receiver_status, TerminalStatus) else "unknown"
    )
    growth = None
    if opener_ref is not None:
        path = opener_ref.get("path")
        inode = opener_ref.get("inode")
        size = opener_ref.get("size")
        if isinstance(path, str) and path and type(size) is int:
            try:
                settled = Path(path).stat()
            except (OSError, ValueError):
                pass
            else:
                growth = {
                    "size_at_open": size,
                    "size_at_settle": settled.st_size,
                    "inode_stable": type(inode) is int and settled.st_ino == inode,
                }
    classified["transcript_growth"] = growth
    return classified


def _confirmation_timeout_seconds() -> float:
    defaults = settings_service.get_provider_defaults("inbox")
    if "confirmation_timeout_seconds" not in defaults:
        return 10.0
    raw = defaults["confirmation_timeout_seconds"]
    try:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError
        timeout = float(raw)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except (OverflowError, TypeError, ValueError):
        logger.warning(
            "Invalid [inbox] confirmation_timeout_seconds=%r; using default 10.0",
            raw,
        )
        return 10.0
    return timeout


_delivery_locks: dict[str, threading.Lock] = {}
_delivery_locks_guard = threading.Lock()
_delivery_wake_seq: dict[str, int] = {}
_delivery_seq_guard = threading.Lock()


# ---------------------------------------------------------------------------
# F136-D15: O(1) wake-admission state machine
# ---------------------------------------------------------------------------


@dataclass
class _WakeState:
    """Per-terminal wake admission state (D15)."""

    dirty_epoch: int = 0
    immediate_admitted: bool = False
    holder_epoch: int = 0
    delayed_token: int = 0
    delayed_handle: Any = None  # asyncio.TimerHandle | None


_wake_states: dict[str, _WakeState] = {}


# Failure backoff state (D16)
_failure_streaks: dict[str, int] = {}
_BACKOFF_SCHEDULE = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


@dataclass
class CallbackRunOutcome:
    """D18: Structured outcome of one callback delivery run."""

    selected: int = 0
    processed: int = 0
    cursor_before: int | None = None
    cursor_after: int | None = None
    replay_selected: int = 0
    replay_drained: int = 0
    written: int = 0
    already_present: int = 0
    retryable_failure_count: int = 0
    identity_conflict_count: int = 0
    bootstrap_mode: str | None = None
    needs_immediate_wake: bool = False
    retry_delay_s: float | None = None
    reason: str = ""
    max_written_row_id: int = 0  # F168 D4: highest row id written this run
    # fx168 FIX-2: stale path heal data (mailbox_id, terminal_id, generation, new_path)
    _fx168_stale_heal: tuple[str, str, int, str] | None = None


def _get_backoff_delay(terminal_id: str) -> float:
    """D16: Compute backoff delay and increment failure streak."""
    streak = _failure_streaks.get(terminal_id, 0)
    _failure_streaks[terminal_id] = streak + 1
    idx = min(streak, len(_BACKOFF_SCHEDULE) - 1)
    return _BACKOFF_SCHEDULE[idx]


def request_delivery(terminal_id: str) -> None:
    """D7/D15: Signal that deliverable work exists for a terminal.

    O(1) admission: increments dirty_epoch, invalidates delayed token,
    and admits at most one immediate callback. Never calls deliver_pending
    inline and never writes the native inbox.
    """
    service = globals().get("inbox_service")
    if not isinstance(service, InboxService):
        return

    # F339: suppress delivery for terminals already abandoned as ghosts.
    if service._f339_is_abandoned(terminal_id):
        return

    old_handle: Any = None
    post_immediate = False

    with _delivery_seq_guard:
        loop = service._delivery_loop
        if loop is None or loop.is_closed():
            return

        state = _wake_states.get(terminal_id)
        if state is None:
            state = _WakeState()
            _wake_states[terminal_id] = state

        state.dirty_epoch += 1
        # Invalidate delayed token
        state.delayed_token += 1
        old_handle = state.delayed_handle
        state.delayed_handle = None

        # Admit one immediate if none running/posted
        if not state.immediate_admitted:
            state.immediate_admitted = True
            post_immediate = True

    # Cancel old timer and post immediate outside guard
    if old_handle is not None:
        old_handle.cancel()

    if post_immediate:
        try:
            loop.call_soon_threadsafe(service._f136_start_delivery_wake, terminal_id)
        except RuntimeError:
            with _delivery_seq_guard:
                st = _wake_states.get(terminal_id)
                if st:
                    st.immediate_admitted = False


@dataclass
class _IdentityAuthorityEpisode:
    count: int = 0
    notified: bool = False
    last_reason: str = "read_error"


def get_delivery_lock(terminal_id: str) -> threading.Lock:
    with _delivery_locks_guard:
        lock = _delivery_locks.get(terminal_id)
        if lock is None:
            lock = threading.Lock()
            _delivery_locks[terminal_id] = lock
        return lock


_get_delivery_lock = get_delivery_lock


def clear_terminal_delivery_state(terminal_id: str) -> None:
    """Clear per-terminal state while retaining permanent delivery-lock identity."""
    with _delivery_seq_guard:
        _delivery_wake_seq.pop(terminal_id, None)
    service = globals().get("inbox_service")
    if isinstance(service, InboxService):
        service._clear_identity_authority(terminal_id)
        service.reset_binding_episodes(terminal_id)
        with service._binding_lock:
            service._binding_suppress_counts.pop(terminal_id, None)
        with service._gone_lock:
            service._gone_streaks.pop(terminal_id, None)
        # F339: clear ghost-terminal streak on explicit state reset.
        with service._tnf_lock:
            service._terminal_not_found_streaks.pop(terminal_id, None)
    clear_binding_staleness_state(terminal_id)


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

    # --- F74: message-state authority helper ---
    @staticmethod
    def _message_statuses(message_ids: list[int]) -> dict[int, str]:
        """Read current MESSAGE-level statuses for a batch of IDs.

        Single query, no lock — advisory pre-check before recovery actions.
        The CAS in recover_wpm2_stale_attempt remains the last-line invariant.
        """
        if not message_ids:
            return {}
        from cli_agent_orchestrator.clients.database import InboxModel, SessionLocal

        with SessionLocal() as db:
            rows = (
                db.query(InboxModel.id, InboxModel.status)
                .filter(InboxModel.id.in_(message_ids))
                .all()
            )
        return {int(row_id): status for row_id, status in rows}

    def __init__(self) -> None:
        self._defer_attempts: dict[int, int] = {}
        self._defer_notified: set[int] = set()
        self._defer_lock = threading.Lock()
        self._identity_authority: dict[tuple[str, str], _IdentityAuthorityEpisode] = {}
        self._identity_lock = threading.Lock()
        self._binding_authority: dict[tuple[str, str], _IdentityAuthorityEpisode] = {}
        # Monotonic per-terminal suppress count for B3 escape (not reset on rebind).
        self._binding_suppress_counts: dict[str, int] = {}
        self._binding_lock = threading.Lock()
        self._gone_streaks: dict[str, int] = {}
        self._gone_lock = threading.Lock()
        # F339: consecutive terminal-not-found streaks for ghost-terminal detection.
        self._terminal_not_found_streaks: dict[str, int] = {}
        self._tnf_lock = threading.Lock()
        self._delivery_loop: asyncio.AbstractEventLoop | None = None
        self._delivery_registry: PluginRegistry | None = None
        self._posted_delivery_wakes: set[tuple[str, int]] = set()
        self._delivery_tasks: set[asyncio.Task[None]] = set()
        self._prestart_wake_logged = False

    @staticmethod
    def _gate_episode(value: object) -> str:
        if isinstance(value, tuple) and len(value) == 2:
            return f"{value[0]}:{value[1]}"
        return f"{value}:-"

    def _inject_safe(
        self,
        terminal_id: str,
        provider: object | None,
        probe_meta: ScreenProbeMeta | dict[str, Any] | object,
    ) -> InjectSafetyResult:
        """Return the closed pre-open safety decision from one final probe."""
        if not isinstance(probe_meta, dict) or probe_meta.get("result_status") not in {
            "waiting_user_answer",
            "error",
            "processing",
            "completed",
            "idle",
            "unknown",
        }:
            logger.warning(
                "inject_safety_probe_failure terminal=%s kind=malformed_meta", terminal_id
            )
            return InjectSafetyResult("veto", "safety_unverified")
        if probe_meta.get("probe_failure") is not None:
            logger.warning(
                "inject_safety_probe_failure terminal=%s kind=%s",
                terminal_id,
                probe_meta.get("probe_failure"),
            )
            return InjectSafetyResult("veto", "safety_unverified")
        if probe_meta["result_status"] == "waiting_user_answer":
            return InjectSafetyResult("veto", "waiting_status")
        try:
            from cli_agent_orchestrator.services.auto_responder import auto_responder

            gate = auto_responder.waiting_gate(terminal_id)
        except Exception:
            logger.warning(
                "inject_safety_probe_failure terminal=%s kind=waiting_gate_exception",
                terminal_id,
                exc_info=True,
            )
            return InjectSafetyResult("veto", "safety_unverified")
        if gate is not None:
            return InjectSafetyResult(
                "veto",
                "waiting_gate",
                self._gate_episode(gate),
            )
        if probe_meta.get("injection_hazard") is not None:
            return InjectSafetyResult("veto", "dialog_hazard")
        if probe_meta.get("identity_proof_failure") is not None:
            return InjectSafetyResult("veto", "identity_unverified")
        if provider is None:
            logger.warning(
                "inject_safety_probe_failure terminal=%s kind=provider_missing", terminal_id
            )
            return InjectSafetyResult("veto", "safety_unverified")
        return InjectSafetyResult("safe")

    def schedule_delivery_wake(self, terminal_id: str) -> bool:
        """Post one coalesced, non-blocking retry onto the owning API loop."""
        with _delivery_seq_guard:
            loop = self._delivery_loop
            registry = self._delivery_registry
            if loop is None or loop.is_closed() or registry is None:
                if not self._prestart_wake_logged:
                    logger.warning("delivery_wake_dropped_service_not_running")
                    self._prestart_wake_logged = True
                return False
            key = (terminal_id, _delivery_wake_seq.get(terminal_id, 0))
            if key in self._posted_delivery_wakes:
                return False
            self._posted_delivery_wakes.add(key)
        try:
            loop.call_soon_threadsafe(self._start_delivery_wake, key)
        except RuntimeError:
            with _delivery_seq_guard:
                self._posted_delivery_wakes.discard(key)
            logger.warning("delivery_wake_dropped_loop_closed terminal=%s", terminal_id)
            return False
        return True

    def _start_delivery_wake(self, key: tuple[str, int]) -> None:
        with _delivery_seq_guard:
            self._posted_delivery_wakes.discard(key)
            registry = self._delivery_registry
        if registry is None:
            return

        async def deliver() -> None:
            await asyncio.to_thread(self.deliver_pending, key[0], registry=registry)

        task = asyncio.create_task(deliver())
        self._delivery_tasks.add(task)

        def completed(value: asyncio.Task[None]) -> None:
            self._delivery_tasks.discard(value)
            try:
                value.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("scheduled delivery wake failed terminal=%s", key[0])

        task.add_done_callback(completed)

    # -------------------------------------------------------------------
    # F136-D13/D15: Callback delivery runner and wake lifecycle
    # -------------------------------------------------------------------

    def _f136_start_delivery_wake(self, terminal_id: str) -> None:
        """D15: Start one delivery run for a terminal on the event loop."""

        async def run_delivery() -> None:
            outcome = await asyncio.to_thread(self._f136_run_callback_delivery, terminal_id)
            self._f136_post_delivery(terminal_id, outcome)

        task = asyncio.create_task(run_delivery())
        self._delivery_tasks.add(task)

        def done(t: asyncio.Task[None]) -> None:
            self._delivery_tasks.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("f136_delivery_wake_failed terminal=%s", terminal_id)

        task.add_done_callback(done)

    def _f136_run_callback_delivery(self, terminal_id: str) -> "CallbackRunOutcome":
        """D13/D14: Bounded replay-first, cursor-second write protocol."""
        # F339: if already abandoned, exit immediately without DB queries.
        if self._f339_is_abandoned(terminal_id):
            return CallbackRunOutcome(reason="abandoned_no_terminal")

        from cli_agent_orchestrator.clients.database import (
            CallbackBatchResult,
            commit_supervisor_callback_progress,
            get_supervisor_callback_batch,
        )
        from cli_agent_orchestrator.services.mailbox_service import (
            get_mailbox_authority_lock,
            is_supervisor_mailbox_pull_terminal,
        )
        from cli_agent_orchestrator.services.teammate_push_service import (
            NativeInboxWriteResult,
            write_supervisor_callback_notification,
        )

        MAX_ROWS_PER_RUN = 50
        MAX_SECONDS_PER_RUN = 0.200

        # Resolve mailbox for this terminal
        from cli_agent_orchestrator.clients.database import (
            MailboxIncarnationModel,
            MailboxModel,
            SessionLocal,
        )

        with SessionLocal() as db:
            inc = db.query(MailboxIncarnationModel).filter_by(terminal_id=terminal_id).one_or_none()
            if inc is None:
                return self._f339_record_not_found(terminal_id, "no_incarnation")
            mailbox = db.query(MailboxModel).filter_by(id=inc.mailbox_id).one_or_none()
            if mailbox is None:
                return self._f339_record_not_found(terminal_id, "no_mailbox")
            mailbox_id = str(mailbox.id)
            generation = int(mailbox.generation)
            session_name = str(mailbox.session_name)
            role = str(mailbox.role)

        # F339: incarnation + mailbox found — reset ghost-terminal streak.
        self._f339_reset_not_found(terminal_id)

        # D10: acquire delivery_lock then authority lock
        delivery_lock = get_delivery_lock(terminal_id)
        if not delivery_lock.acquire(timeout=0.5):
            return CallbackRunOutcome(reason="delivery_lock_contention")

        authority_lock = get_mailbox_authority_lock(session_name, role)
        if not authority_lock.acquire(timeout=0.5):
            delivery_lock.release()
            return CallbackRunOutcome(reason="authority_lock_contention")

        try:
            deadline_mono = time.monotonic() + MAX_SECONDS_PER_RUN

            # D9: Get batch
            batch = get_supervisor_callback_batch(
                mailbox_id=mailbox_id,
                terminal_id=terminal_id,
                generation=generation,
                limit=MAX_ROWS_PER_RUN,
            )

            if batch.kind == "stale_authority":
                return CallbackRunOutcome(reason=f"stale: {batch.reason}")
            if batch.kind == "retryable_failure":
                return CallbackRunOutcome(
                    retryable_failure_count=1,
                    reason=batch.reason,
                    retry_delay_s=_get_backoff_delay(terminal_id),
                )
            if batch.kind == "no_path":
                # F150: Self-heal — attempt to re-discover inbox path from
                # terminal metadata before giving up.
                healed = self._f150_self_heal_inbox_path(
                    mailbox_id=mailbox_id,
                    terminal_id=terminal_id,
                    generation=generation,
                )
                if healed:
                    # Re-fetch batch now that path is populated
                    batch = get_supervisor_callback_batch(
                        mailbox_id=mailbox_id,
                        terminal_id=terminal_id,
                        generation=generation,
                        limit=MAX_ROWS_PER_RUN,
                    )
                    if batch.kind == "no_path":
                        # Self-heal wrote a path but it didn't stick — give up
                        return CallbackRunOutcome(
                            cursor_before=batch.cursor,
                            reason="no_path",
                            retry_delay_s=_get_backoff_delay(terminal_id),
                            retryable_failure_count=1,
                        )
                else:
                    return CallbackRunOutcome(
                        cursor_before=batch.cursor,
                        reason="no_path",
                        retry_delay_s=_get_backoff_delay(terminal_id),
                        retryable_failure_count=1,
                    )
            if not batch.rows:
                # Empty scan: reset failure streak
                _failure_streaks.pop(terminal_id, None)
                return CallbackRunOutcome(
                    cursor_before=batch.cursor,
                    cursor_after=batch.cursor,
                    bootstrap_mode=batch.bootstrap_mode,
                    reason="empty",
                )

            # fx168 FIX-2: Staleness-aware self-heal — if the batch's canonical
            # inbox_path differs from the CURRENT terminal metadata cc_team_inbox_path,
            # exit the lock scope and reconcile via set_supervisor_callback_inbox_path
            # (which acquires its own locks, bumps version, re-kicks request_delivery).
            # One attempt per run; the re-wake handles the actual write.
            _fx168_stale_heal_needed: tuple[str, str] | None = None
            if batch.inbox_path:
                try:
                    from cli_agent_orchestrator.clients.database import (
                        get_terminal_metadata as _get_meta,
                    )

                    _meta = _get_meta(terminal_id)
                    _current_path = (
                        (_meta.get("metadata") or {}).get("cc_team_inbox_path") if _meta else None
                    )
                    if _current_path and _current_path != batch.inbox_path:
                        _fx168_stale_heal_needed = (batch.inbox_path, _current_path)
                except Exception as _heal_exc:
                    logger.debug(
                        "fx168_stale_path_check_failed terminal=%s: %s", terminal_id, _heal_exc
                    )

            if _fx168_stale_heal_needed is not None:
                # Cannot reconcile while holding locks (set_supervisor_callback_inbox_path
                # acquires the same locks). Signal stale_path_detected; the reconcile
                # happens in _f136_post_delivery after lock release.
                _old_path, _new_path = _fx168_stale_heal_needed
                return CallbackRunOutcome(
                    cursor_before=batch.cursor,
                    needs_immediate_wake=True,
                    reason="stale_path_detected",
                    _fx168_stale_heal=(mailbox_id, terminal_id, generation, _new_path),
                )

            inbox_path = Path(os.path.expanduser(batch.inbox_path))
            cursor_before = batch.cursor
            new_cursor = batch.cursor or 0
            successful_replay_ids: list[int] = []
            written = 0
            already_present = 0
            retryable_failures = 0
            identity_conflicts = 0
            processed = 0
            _max_written_row_id = 0  # F168 D4: track highest row id written

            # D13: Process replay rows first, then forward rows
            for row in batch.rows:
                if time.monotonic() >= deadline_mono:
                    break

                msg = InboxMessage(
                    id=row.inbox_row_id,
                    sender_id=row.sender_id,
                    receiver_id=terminal_id,
                    message=row.message,
                    orchestration_type=OrchestrationType.SEND_MESSAGE,
                    status=MessageStatus.PENDING,
                    created_at=row.created_at,
                )
                result = write_supervisor_callback_notification(
                    inbox_path=inbox_path,
                    mailbox_id=mailbox_id,
                    message=msg,
                    deadline_mono=deadline_mono,
                )
                processed += 1

                if result.kind == "written":
                    written += 1
                    if row.inbox_row_id > _max_written_row_id:
                        _max_written_row_id = row.inbox_row_id
                elif result.kind == "already_present":
                    already_present += 1
                elif result.kind == "retryable_failure":
                    retryable_failures += 1
                    break  # Stop at first failure
                elif result.kind == "identity_conflict":
                    identity_conflicts += 1
                    logger.error(
                        "f136_identity_conflict mailbox=%s row=%s reason=%s",
                        mailbox_id,
                        row.inbox_row_id,
                        result.reason,
                    )
                    break  # Stop at conflict

                # Track progress
                if result.kind in ("written", "already_present"):
                    if row.tag == "replay":
                        successful_replay_ids.append(row.inbox_row_id)
                    elif row.tag == "forward":
                        # Forward rows are batch-ordered ascending; all are eligible.
                        # Advance cursor through every successfully written row.
                        new_cursor = row.inbox_row_id

            # D13 step 5: commit progress once
            if successful_replay_ids or new_cursor > (cursor_before or 0):
                progress = commit_supervisor_callback_progress(
                    mailbox_id=mailbox_id,
                    terminal_id=terminal_id,
                    generation=generation,
                    expected_cursor=cursor_before or 0,
                    new_cursor=new_cursor,
                    expected_path_version=batch.path_version,
                    replay_row_ids=tuple(successful_replay_ids),
                )
                if progress.kind == "advanced":
                    _failure_streaks.pop(terminal_id, None)
                elif progress.kind == "path_changed":
                    return CallbackRunOutcome(
                        selected=len(batch.rows),
                        processed=processed,
                        cursor_before=cursor_before,
                        cursor_after=cursor_before,
                        written=written,
                        already_present=already_present,
                        reason="path_changed_during_run",
                        needs_immediate_wake=True,
                    )
                else:
                    retryable_failures += 1
            else:
                _failure_streaks.pop(terminal_id, None)

            replay_selected = sum(1 for r in batch.rows if r.tag == "replay")
            needs_wake = batch.has_more or (retryable_failures == 0 and processed < len(batch.rows))

            return CallbackRunOutcome(
                selected=len(batch.rows),
                processed=processed,
                cursor_before=cursor_before,
                cursor_after=new_cursor if new_cursor > (cursor_before or 0) else cursor_before,
                replay_selected=replay_selected,
                replay_drained=len(successful_replay_ids),
                written=written,
                already_present=already_present,
                retryable_failure_count=retryable_failures,
                identity_conflict_count=identity_conflicts,
                bootstrap_mode=batch.bootstrap_mode,
                needs_immediate_wake=needs_wake,
                retry_delay_s=_get_backoff_delay(terminal_id) if retryable_failures else None,
                reason="ok",
                max_written_row_id=_max_written_row_id,
            )
        except Exception as exc:
            logger.exception("f136_delivery_run_error terminal=%s", terminal_id)
            return CallbackRunOutcome(
                retryable_failure_count=1,
                reason=f"exception: {exc}",
                retry_delay_s=_get_backoff_delay(terminal_id),
            )
        finally:
            authority_lock.release()
            delivery_lock.release()

    def _f150_self_heal_inbox_path(
        self, *, mailbox_id: str, terminal_id: str, generation: int
    ) -> bool:
        """F150: Attempt to re-discover and populate cc_inbox_path from terminal metadata.

        Returns True if a path was found and set, False otherwise.
        """
        from cli_agent_orchestrator.clients.database import get_terminal_metadata
        from cli_agent_orchestrator.services.mailbox_service import (
            set_supervisor_callback_inbox_path,
        )

        try:
            meta_record = get_terminal_metadata(terminal_id)
            if not meta_record:
                # F339: terminal metadata absent — record streak toward ghost detection.
                # Note: we don't abandon here (the caller handles retries), but we
                # increment so repeated failures across delivery attempts accumulate.
                self._f339_record_not_found(terminal_id, "no_terminal_metadata_f150")
                return False
            # F339: terminal metadata found — reset ghost-terminal streak.
            self._f339_reset_not_found(terminal_id)
            md = meta_record.get("metadata") or {}
            candidate_path = md.get("cc_team_inbox_path")
            if not candidate_path:
                return False
            result = set_supervisor_callback_inbox_path(
                mailbox_id=mailbox_id,
                terminal_id=terminal_id,
                generation=generation,
                path=candidate_path,
            )
            return result.kind in ("updated", "unchanged")
        except Exception as exc:
            logger.debug("F150 self-heal inbox path failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # F339: Ghost-terminal detection — stop polling after N consecutive 404s
    # ------------------------------------------------------------------

    def _f339_record_not_found(self, terminal_id: str, reason: str) -> "CallbackRunOutcome":
        """Increment ghost-terminal streak; abandon episode at threshold."""
        with self._tnf_lock:
            streak = self._terminal_not_found_streaks.get(terminal_id, 0) + 1
            self._terminal_not_found_streaks[terminal_id] = streak

        if streak >= _F339_TERMINAL_NOT_FOUND_MAX:
            logger.warning(
                "f339_abandoned_no_terminal terminal=%s reason=%s streak=%d"
                " — delivery permanently stopped for this episode",
                terminal_id,
                reason,
                streak,
            )
            # Clear wake state so no further retries are scheduled
            with _delivery_seq_guard:
                _wake_states.pop(terminal_id, None)
            return CallbackRunOutcome(reason="abandoned_no_terminal")
        return CallbackRunOutcome(reason=reason)

    def _f339_reset_not_found(self, terminal_id: str) -> None:
        """Reset ghost-terminal streak on successful terminal lookup."""
        with self._tnf_lock:
            self._terminal_not_found_streaks.pop(terminal_id, None)

    def _f339_is_abandoned(self, terminal_id: str) -> bool:
        """Check if a terminal has been marked as abandoned (ghost)."""
        with self._tnf_lock:
            return (
                self._terminal_not_found_streaks.get(terminal_id, 0) >= _F339_TERMINAL_NOT_FOUND_MAX
            )

    def _f136_post_delivery(self, terminal_id: str, outcome: "CallbackRunOutcome") -> None:
        """D15: After delivery, decide: immediate rerun, delayed retry, or idle."""
        # F339: If the episode was abandoned (ghost terminal), suppress all retries.
        if outcome.reason == "abandoned_no_terminal":
            return

        # fx168 FIX-2: Perform stale-path reconcile outside the runner's lock scope.
        # set_supervisor_callback_inbox_path acquires its own locks and signals
        # request_delivery internally; the needs_immediate_wake flag re-arms the runner.
        if outcome._fx168_stale_heal is not None:
            try:
                _mb_id, _term_id, _gen, _new_path = outcome._fx168_stale_heal
                from cli_agent_orchestrator.services.mailbox_service import (
                    set_supervisor_callback_inbox_path,
                )

                _heal_result = set_supervisor_callback_inbox_path(
                    mailbox_id=_mb_id,
                    terminal_id=_term_id,
                    generation=_gen,
                    path=_new_path,
                )
                if _heal_result.kind in ("updated", "unchanged"):
                    logger.info(
                        "fx168_stale_path_healed mailbox=%s terminal=%s new_path=%s",
                        _mb_id,
                        _term_id,
                        _new_path,
                    )
            except Exception as _heal_exc:
                logger.debug(
                    "fx168_stale_path_heal_post_delivery_failed terminal=%s: %s",
                    terminal_id,
                    _heal_exc,
                )

        # F168 D2: ring the doorbell before entering _delivery_seq_guard.
        # D3: best-effort, isolated — exceptions never propagate.
        if outcome.written > 0 and outcome.max_written_row_id > 0:
            try:
                from cli_agent_orchestrator.services.doorbell_service import (
                    ring_supervisor_doorbell,
                )

                ring_supervisor_doorbell(
                    terminal_id,
                    outcome.max_written_row_id,
                    written_count=outcome.written,
                )
            except Exception as _bell_exc:
                logger.debug(
                    "f168_doorbell_post_delivery_error terminal=%s: %s", terminal_id, _bell_exc
                )

        post_immediate = False
        arm_delayed: float | None = None

        with _delivery_seq_guard:
            state = _wake_states.get(terminal_id)
            if state is None:
                return

            if outcome.needs_immediate_wake:
                # More work available — rerun immediately
                state.holder_epoch = state.dirty_epoch
                post_immediate = True
            elif outcome.retry_delay_s is not None:
                # Failure — arm delayed retry
                state.immediate_admitted = False
                if state.dirty_epoch > state.holder_epoch:
                    # New work arrived during run — immediate instead
                    state.immediate_admitted = True
                    post_immediate = True
                else:
                    arm_delayed = outcome.retry_delay_s
            elif state.dirty_epoch > state.holder_epoch:
                # New work arrived during our run — one more immediate
                state.holder_epoch = state.dirty_epoch
                post_immediate = True
            else:
                # Idle
                state.immediate_admitted = False

        loop = self._delivery_loop
        if loop is None or loop.is_closed():
            return

        if post_immediate:
            try:
                loop.call_soon_threadsafe(self._f136_start_delivery_wake, terminal_id)
            except RuntimeError:
                with _delivery_seq_guard:
                    st = _wake_states.get(terminal_id)
                    if st:
                        st.immediate_admitted = False
        elif arm_delayed is not None:
            try:
                loop.call_soon_threadsafe(self._f136_arm_delayed, terminal_id, arm_delayed)
            except RuntimeError:
                pass

    def _f136_arm_delayed(self, terminal_id: str, delay: float) -> None:
        """Arm a delayed delivery wake on the event loop."""
        with _delivery_seq_guard:
            state = _wake_states.get(terminal_id)
            if state is None:
                return
            token = state.delayed_token
            state.delayed_handle = asyncio.get_event_loop().call_later(
                delay, self._f136_delayed_fire, terminal_id, token
            )

    def _f136_delayed_fire(self, terminal_id: str, token: int) -> None:
        """Fire a delayed wake if the token is still valid."""
        with _delivery_seq_guard:
            state = _wake_states.get(terminal_id)
            if state is None:
                return
            if state.delayed_token != token:
                return  # Superseded
            state.delayed_handle = None
            if not state.immediate_admitted:
                state.immediate_admitted = True
            else:
                return  # Already running

        self._f136_start_delivery_wake(terminal_id)

    def _clear_identity_authority(self, terminal_id: str) -> None:
        with self._identity_lock:
            for key in [key for key in self._identity_authority if key[0] == terminal_id]:
                self._identity_authority.pop(key, None)

    @staticmethod
    def _identity_authority_token(
        batch: Sequence[InboxMessage],
        attempt_uuid: str | None = None,
        routed_generation: int | None = None,
    ) -> str:
        logical_id = getattr(batch[0], "logical_receiver_id", None) if batch else None
        if not isinstance(logical_id, str) or not logical_id.startswith("mb_"):
            return "raw"
        if attempt_uuid is not None:
            authority = get_attempt_mailbox_authority(attempt_uuid)
            if authority is not None and type(authority.get("generation")) is int:
                return str(authority["generation"])
        generation = getattr(batch[0], "enqueue_generation", None)
        if type(generation) is not int:
            generation = routed_generation
        if type(generation) is not int:
            raise RuntimeError("mailbox_generation_unavailable")
        return str(generation)

    @staticmethod
    def _evidence_for_confirmed_attempt(
        terminal_id: str, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        """Tag confirmed-settlement evidence when the binding is presumed_stale."""
        if binding_presumed_stale(terminal_id):
            return {**evidence, "kind": "binding_presumed_stale"}
        return dict(evidence)

    @staticmethod
    def _identity_notice_receiver(terminal_id: str, metadata: dict[str, Any]) -> str | None:
        caller_id = metadata.get("caller_id")
        if isinstance(caller_id, str) and get_terminal_metadata(caller_id) is not None:
            return caller_id
        session_name = metadata.get("tmux_session")
        if not isinstance(session_name, str):
            return None
        from cli_agent_orchestrator.clients.database import list_terminals_by_session
        from cli_agent_orchestrator.services.fleet_service import build_fleet
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        try:
            fleet = build_fleet(session_name)
        except ValueError:
            return None
        fleet_terminals = list(fleet.get("terminals", []))
        live_ids = {
            str(item["id"])
            for item in fleet_terminals
            if item.get("status") != TerminalStatus.ERROR.value and not item.get("orphan")
        }
        candidates: list[dict[str, Any]] = []
        for row in list_terminals_by_session(session_name):
            row_id = row.get("id")
            if row_id == terminal_id or row_id not in live_ids:
                continue
            try:
                profile = load_agent_profile(row.get("agent_profile") or "")
            except (FileNotFoundError, ValueError):
                continue
            if getattr(profile, "role", None) == "supervisor":
                candidates.append(row)
        if not candidates:
            return None

        # Parent → most-recent-live → critical. Prefer the subject's own live
        # supervisor (metadata caller_id / fleet parent_id) before last_active.
        parent_id = metadata.get("caller_id")
        if not isinstance(parent_id, str):
            subject = next(
                (item for item in fleet_terminals if str(item.get("id")) == terminal_id),
                None,
            )
            parent_id = subject.get("parent_id") if subject is not None else None
        if isinstance(parent_id, str) and parent_id in live_ids:
            for candidate in candidates:
                if str(candidate.get("id")) == parent_id:
                    return parent_id

        def _last_active_key(row: dict[str, Any]) -> datetime:
            value = row.get("last_active")
            if not isinstance(value, datetime):
                return datetime.min.replace(tzinfo=timezone.utc)
            if value.tzinfo is None:
                return value.replace(tzinfo=get_localzone(), fold=0).astimezone(timezone.utc)
            return value.astimezone(timezone.utc)

        candidates.sort(key=_last_active_key, reverse=True)
        return str(candidates[0]["id"])

    def _record_identity_authority_failure(
        self,
        terminal_id: str,
        batch: Sequence[InboxMessage],
        metadata: dict[str, Any],
        reason: str,
        *,
        attempt_uuid: str | None = None,
        routed_generation: int | None = None,
    ) -> None:
        token = self._identity_authority_token(batch, attempt_uuid, routed_generation)
        key = (terminal_id, token)
        with self._identity_lock:
            for old in [
                item for item in self._identity_authority if item[0] == terminal_id and item != key
            ]:
                self._identity_authority.pop(old, None)
            episode = self._identity_authority.setdefault(key, _IdentityAuthorityEpisode())
            episode.count += 1
            episode.last_reason = reason
            if episode.count < 3 or episode.notified:
                return
            count = episode.count

        receiver = self._identity_notice_receiver(terminal_id, metadata)
        if receiver is None:
            logger.critical(
                "identity_authority_lost terminal=%s reason=%s count=%s no_supervisor",
                terminal_id,
                reason,
                count,
            )
            with self._identity_lock:
                self._identity_authority[key].notified = True
            return
        body = (
            f"[identity-authority] terminal {terminal_id} pane identity unverifiable "
            f"({reason}, x{count})\nHuman attention is required; delivery remains pending."
        )
        outcome = insert_identity_authority_notice(f"message-trace:{terminal_id}", receiver, body)
        if outcome != NoticeInsertOutcome.FAILED_BEFORE_COMMIT:
            with self._identity_lock:
                self._identity_authority[key].notified = True
        else:
            logger.error(
                "identity_authority_notice_failed_before_commit terminal=%s receiver=%s",
                terminal_id,
                receiver,
            )

    def _reset_identity_authority(self, terminal_id: str) -> None:
        self._clear_identity_authority(terminal_id)

    def reset_binding_episodes(self, terminal_id: str) -> None:
        """Clear only the binding-authority family for one terminal."""
        with self._binding_lock:
            for key in [key for key in self._binding_authority if key[0] == terminal_id]:
                self._binding_authority.pop(key, None)

    def _record_binding_authority_failure(
        self, terminal_id: str, binding_id: int, metadata: dict[str, Any]
    ) -> None:
        key = (terminal_id, f"binding:{binding_id}")
        with self._binding_lock:
            episode = self._binding_authority.setdefault(key, _IdentityAuthorityEpisode())
            episode.count += 1
            if episode.count < 3 or episode.notified:
                return
            count = episode.count
        receiver = self._identity_notice_receiver(terminal_id, metadata)
        if receiver is None:
            logger.critical(
                "binding_authority_lost terminal=%s binding=%s count=%s no_supervisor",
                terminal_id,
                binding_id,
                count,
            )
            with self._binding_lock:
                self._binding_authority[key].notified = True
            return
        body = (
            f"[binding-authority] transcript binding presumed stale for terminal {terminal_id} "
            f"(binding {binding_id}): delivery confirmations unconfirmable; {count} cycles "
            "suppressed; awaiting binding recovery or a new session epoch"
        )
        outcome = insert_identity_authority_notice(f"message-trace:{terminal_id}", receiver, body)
        if outcome != NoticeInsertOutcome.FAILED_BEFORE_COMMIT:
            with self._binding_lock:
                self._binding_authority[key].notified = True
        else:
            logger.error(
                "binding_authority_notice_failed_before_commit terminal=%s receiver=%s",
                terminal_id,
                receiver,
            )

    def _resolve_stale_binding_prior_hits(
        self,
        terminal_id: str,
        metadata: dict[str, Any],
        prior_lookups: Sequence[tuple[dict[str, Any], str, dict[str, Any]]],
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any], str | None] | None:
        """Resolve one presumed-stale binding before any new attempt can open.

        Compact/clear rotations are first-class: when a compact binding points at
        a different transcript path, rebind and stop suppressing. Suppression is
        bounded by BINDING_SUPPRESS_MAX_CYCLES — after that, return None so
        delivery may open without transcript confirmation (unverified path).
        """
        if not any(result == "absent" for _, result, _ in prior_lookups):
            return None
        stale = observe_binding_absence(metadata)
        if stale is None or not stale.presumed_stale:
            return None

        compact = get_latest_compact_transcript_binding(terminal_id)
        if compact is not None:
            compact_path = Path(str(compact.get("transcript_path") or "")).resolve(strict=False)
            if compact_path != stale.path and str(compact_path):
                recovery = recover_transcript_binding_if_current(
                    terminal_id, stale.binding_id, str(compact_path)
                )
                if recovery in {"inserted", "authority_changed"}:
                    clear_binding_staleness_state(terminal_id)
                    for prior, _, prior_evidence in prior_lookups:
                        refreshed, refreshed_evidence = _wpm2_lookup(
                            metadata,
                            prior["payload_hash"],
                            prior.get("started_at"),
                            prior_evidence,
                        )
                        if refreshed == "hit":
                            return "hit", prior, refreshed_evidence, str(compact_path)
                    # Rotation followed; allow a fresh delivery attempt to open.
                    return None

        for prior, _, prior_evidence in prior_lookups:
            candidate_result, candidate_evidence, candidate = scan_binding_candidates(
                stale,
                prior["payload_hash"],
                prior.get("started_at"),
                prior_evidence,
            )
            if candidate_result != "hit" or candidate is None:
                continue
            recovery = recover_transcript_binding_if_current(
                terminal_id, stale.binding_id, str(candidate)
            )
            if recovery == "authority_changed":
                refreshed, refreshed_evidence = _wpm2_lookup(
                    metadata,
                    prior["payload_hash"],
                    prior.get("started_at"),
                    prior_evidence,
                )
                if refreshed == "hit":
                    return "hit", prior, refreshed_evidence, None
                return "authority_changed", None, {}, None
            return (
                "hit",
                prior,
                {**candidate_evidence, "kind": "binding_presumed_stale"},
                str(candidate),
            )
        self._record_binding_authority_failure(terminal_id, stale.binding_id, metadata)
        # Escape counter is per-terminal (not per binding_id): rebind/POST must not
        # restart the N-cycle climb and starve the deadlock break (S3).
        with self._binding_lock:
            count = self._binding_suppress_counts.get(terminal_id, 0) + 1
            self._binding_suppress_counts[terminal_id] = count
        if count >= BINDING_SUPPRESS_MAX_CYCLES:
            logger.error(
                "binding_authority_suppress_exhausted terminal=%s binding=%s count=%s "
                "allowing_unverified_delivery",
                terminal_id,
                stale.binding_id,
                count,
            )
            # Do NOT clear _declared_presumed_stale: escaped delivery must still
            # tag kind=binding_presumed_stale so mailbox reports confirmed_unverified (S1).
            return None
        return "suppressed", None, {}, None

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
                "available for notification",
                reason,
                terminal_id,
                message_ids,
            )
            return
        if reason == "receiver_gone":
            body = (
                f"[message-trace] delivery to terminal {terminal_id} failed because the "
                f"receiver terminal no longer exists for message(s) {message_ids}."
            )
        else:
            body = (
                f"[message-trace] delivery to terminal {terminal_id} failed after 3 "
                f"ambiguous attempts for message(s) {message_ids}; inspect cao messages trace."
            )
        create_inbox_message(
            f"message-trace:{terminal_id}",
            caller_id,
            body,
        )

    def _commit_watchdog_ops(
        self,
        terminal_id: str,
        sender_id: str,
        orchestration_type: OrchestrationType,
        metadata: dict,
        park_warm: bool = False,
    ) -> None:
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            stalled_callback_watchdog,
        )

        if sender_id.startswith("watchdog:"):
            return
        stalled_callback_watchdog.record_callback_if_to_caller(sender_id, terminal_id)
        if (
            metadata.get("caller_id")
            and not park_warm
            and (
                orchestration_type == OrchestrationType.ASSIGN
                or (
                    orchestration_type == OrchestrationType.SEND_MESSAGE
                    and sender_id == metadata["caller_id"]
                    and stalled_callback_watchdog.has_episode(terminal_id)
                )
            )
        ):
            stalled_callback_watchdog.record_inbound_task(
                terminal_id, metadata["caller_id"], metadata.get("agent_profile") or ""
            )

    def _settle_probable_delivered(
        self,
        attempt_uuid: str,
        message_ids: list[int],
        batch: Sequence[InboxMessage],
        evidence: dict[str, Any],
        opener_ref: dict[str, Any] | None,
        terminal_id: str,
        sender_id: str,
        orchestration_type: OrchestrationType,
        metadata: dict[str, Any],
        park_warm: bool,
    ) -> tuple[bool, dict[str, Any]]:
        classified = _classify_probable_delivery(evidence, terminal_id, opener_ref)
        if not is_probable_delivered(classified):
            return False, classified
        settlement_evidence = self._evidence_for_confirmed_attempt(
            terminal_id, {**classified, "kind": "execution_evidence"}
        )
        ordered_ids = sorted(message_ids)
        won = _confirmed_settlement(
            lambda: settle_attempt_inferred_delivered_batch(
                attempt_uuid,
                ordered_ids,
                settlement_evidence,
                on_confirmed=lambda: self._commit_watchdog_ops(
                    terminal_id,
                    sender_id,
                    orchestration_type,
                    metadata,
                    park_warm,
                ),
            )
        )
        if not won:
            return False, classified
        growth = settlement_evidence["transcript_growth"]
        assert isinstance(growth, dict)
        growth_bytes = growth["size_at_settle"] - growth["size_at_open"]
        logger.info(
            "probable_delivered mids=%s receiver=%s growth=%s",
            ordered_ids,
            terminal_id,
            growth_bytes,
        )
        self._evict_defer_state(batch)
        return True, settlement_evidence

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
        self,
        terminal_id: str,
        batch,
        metadata: dict,
        provider,
        sender_id: str,
        orchestration_type: OrchestrationType,
        *,
        park_warm: bool = False,
        observe_binding_staleness: bool = True,
    ) -> tuple[str, object | None]:
        """Return normal, stop, or inject for a frozen-law gated batch."""
        message_ids = [message.id for message in batch]
        attempts = self._exact_batch_attempts(message_ids)
        ambiguous = [
            attempt
            for attempt in attempts
            if attempt.get("outcome") == "ambiguous"
            and attempt.get("reason") == "confirmation_timeout"
        ]
        if not ambiguous:
            return "normal", None
        # D1.1 is deliberately before continuity/evidence decoding. Historical
        # malformed rows must not make a dead receiver look non-authoritative.
        if not metadata and any(item.get("provider") == "claude_code" for item in ambiguous):
            result = settle_wpm1_terminal_batch(
                message_ids, MessageStatus.DELIVERY_FAILED, terminal_id, reason="receiver_gone"
            )
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
            metadata.get("provider") == "claude_code"
            or any(item.get("provider") == "claude_code" for item in ambiguous)
        ) and (
            getattr(resolution, "resolution_kind", None) == "binding"
            or any(value.get("resolution_kind") == "binding" for value in decoded.values())
        )
        if not authoritative:
            return "normal", resolution

        newest = ambiguous[-1]
        now = _utcnow()
        now_z = now.isoformat().replace("+00:00", "Z")

        lookup_result = "unresolved"
        prior_lookups: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        successor_plans: list[SuccessorLookupPlan] = []
        for prior in reversed(ambiguous):
            prior_evidence = decoded[prior["attempt_uuid"]]
            evidence_snapshot = copy.deepcopy(prior_evidence)
            lookup_result, lookup_evidence = _wpm2_lookup(
                metadata,
                prior["payload_hash"],
                prior.get("started_at"),
                prior_evidence,
            )
            successor_plans.append(
                _successor_lookup_plan(
                    prior, evidence_snapshot, lookup_result, lookup_evidence, metadata
                )
            )
            prior_lookups.append((prior, lookup_result, prior_evidence))
            if lookup_result == "hit":
                result = _confirmed_settlement(
                    lambda: settle_wpm1_terminal_batch(
                        message_ids,
                        MessageStatus.DELIVERED,
                        terminal_id,
                        confirmation_evidence=(prior["attempt_uuid"], lookup_evidence),
                        on_confirmed=lambda: self._commit_watchdog_ops(
                            terminal_id,
                            sender_id,
                            orchestration_type,
                            metadata,
                            park_warm,
                        ),
                    )
                )
                return "stop", None
            corroboration = lookup_evidence.get("queue_corroboration")
            if corroboration is not None:
                merge_wpm1_attempt_evidence(
                    prior["attempt_uuid"], message_ids, {"queue_corroboration": corroboration}
                )
            if lookup_result == "absent" and lookup_evidence.get("last_observed_ref"):
                _, expected = wpm2_cursor_baseline(prior_evidence)
                if expected is None:
                    return "stop", None
                advanced = advance_wpm2_continuity_cursor(
                    prior["attempt_uuid"],
                    message_ids,
                    expected,
                    lookup_evidence["last_observed_ref"],
                )
                if advanced not in {"advanced", "already_advanced"}:
                    return "stop", None
                prior_evidence["last_observed_ref"] = lookup_evidence["last_observed_ref"]

        stale_resolution = (
            self._resolve_stale_binding_prior_hits(terminal_id, metadata, prior_lookups)
            if observe_binding_staleness
            else None
        )
        if stale_resolution is not None:
            state, stale_prior, stale_evidence, _ = stale_resolution
            if state == "hit" and stale_prior is not None:
                _confirmed_settlement(
                    lambda: settle_wpm1_terminal_batch(
                        message_ids,
                        MessageStatus.DELIVERED,
                        terminal_id,
                        confirmation_evidence=(stale_prior["attempt_uuid"], stale_evidence),
                        on_confirmed=lambda: self._commit_watchdog_ops(
                            terminal_id,
                            sender_id,
                            orchestration_type,
                            metadata,
                            park_warm,
                        ),
                    )
                )
            return "stop", None

        try:
            snapshot = status_monitor.get_boundary_observation(terminal_id)
            if not isinstance(getattr(snapshot, "status", None), TerminalStatus) or not isinstance(
                getattr(snapshot, "observation_epoch", None), str
            ):
                snapshot = None
        except Exception:
            snapshot = None
        status = snapshot.status if snapshot is not None else status_monitor.get_status(terminal_id)
        newest_evidence = decoded[newest["attempt_uuid"]]
        protection = classify_permanently_d2_only(
            newest, snapshot.observation_epoch if snapshot is not None else None
        )
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
        if merge_wpm1_attempt_evidence(newest["attempt_uuid"], message_ids, updates) is not True:
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
            TerminalStatus.IDLE,
            TerminalStatus.COMPLETED,
        }
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
                and type(non_ready) is int
                and non_ready > anchor["seq"]
                and type(ready) is int
                and ready > non_ready
            )
        if gate_open:
            fresh, fresh_evidence = _wpm2_lookup(
                metadata, newest["payload_hash"], newest.get("started_at"), newest_evidence
            )
            if fresh == "hit":
                result = _confirmed_settlement(
                    lambda: settle_wpm1_terminal_batch(
                        message_ids,
                        MessageStatus.DELIVERED,
                        terminal_id,
                        confirmation_evidence=(newest["attempt_uuid"], fresh_evidence),
                        on_confirmed=lambda: self._commit_watchdog_ops(
                            terminal_id,
                            sender_id,
                            orchestration_type,
                            metadata,
                            park_warm,
                        ),
                    )
                )
                return "stop", None
            if fresh == "absent":
                unexhausted = next(
                    (
                        attempt
                        for attempt in reversed(ambiguous)
                        if not decoded[attempt["attempt_uuid"]].get("boundary_exhausted_at")
                    ),
                    None,
                )
                if unexhausted is not None:
                    boundary_snapshot = {
                        "observation_epoch": (snapshot.observation_epoch if snapshot else "legacy"),
                        "status": status.value,
                        "status_gen": (
                            snapshot.status_gen
                            if snapshot
                            else status_monitor.get_status_gen(terminal_id)
                        ),
                        "input_gen": (
                            snapshot.input_gen
                            if snapshot
                            else status_monitor.get_input_gen(terminal_id)
                        ),
                        "seq": (snapshot.seq if snapshot else 0),
                        "last_non_ready_seq": (snapshot.last_non_ready_seq if snapshot else None),
                        "last_ready_seq": (snapshot.last_ready_seq if snapshot else None),
                    }
                    if (
                        merge_wpm1_attempt_evidence(
                            unexhausted["attempt_uuid"],
                            message_ids,
                            {
                                "boundary_exhausted_at": now_z,
                                "boundary_snapshot": boundary_snapshot,
                            },
                        )
                        is not True
                    ):
                        return "stop", None
                    decoded[unexhausted["attempt_uuid"]]["boundary_exhausted_at"] = now_z
                exhausted = sum(
                    bool(decoded[item["attempt_uuid"]].get("boundary_exhausted_at"))
                    for item in ambiguous
                )
                if exhausted >= 3:
                    barrier, _ = _wpm2_lookup(
                        metadata,
                        newest["payload_hash"],
                        newest.get("started_at"),
                        decoded[newest["attempt_uuid"]],
                    )
                    if barrier == "hit":
                        result = _confirmed_settlement(
                            lambda: settle_wpm1_terminal_batch(
                                message_ids,
                                MessageStatus.DELIVERED,
                                terminal_id,
                                on_confirmed=lambda: self._commit_watchdog_ops(
                                    terminal_id,
                                    sender_id,
                                    orchestration_type,
                                    metadata,
                                    park_warm,
                                ),
                            )
                        )
                    elif barrier == "absent":
                        result = settle_wpm1_terminal_batch(
                            message_ids, MessageStatus.DELIVERY_FAILED, terminal_id
                        )
                        if result == "settled":
                            self._notify_delivery_failed(terminal_id, message_ids)
                    return "stop", None
                successors = [
                    item
                    for item in attempts
                    if item.get("prior_attempt_uuid") == newest["attempt_uuid"]
                ]
                if successors:
                    if all(attempt_proven_pre_paste(item) for item in successors):
                        return "normal", {"_wpm1_retry_pre_paste": True}
                    return "stop", None
                evidence = transcript_ref(resolution)
                evidence["boundary_authorized"] = now_z
                evidence["_wpm1_prior_attempt_uuid"] = newest["attempt_uuid"]
                evidence["_successor_lookup_plans"] = tuple(successor_plans)
                return "inject", evidence

        # Threshold decisions are deliberately after every proof/terminal arm.
        is_notice = any(
            str(message.sender_id).startswith("message-trace:")
            and str(message.message).startswith("wpm1-notice ")
            for message in batch
        )
        already_notified = any(
            decoded[item["attempt_uuid"]].get("stalled_notified_at") for item in ambiguous
        )
        if notice_due and not already_notified and not is_notice:
            outcome = record_wpm1_stalled_notice(
                newest["attempt_uuid"], message_ids, terminal_id, now_z
            )
            if outcome == "busy_aborted":
                return "stop", None
        if protection != "normal":
            return "skip_d2_only", {
                "attempt_uuid": newest["attempt_uuid"],
                "member_ids": message_ids,
                "protection_reason": protection,
            }
        return "stop", None

    async def run(self, registry: PluginRegistry | None = None) -> None:
        queue = bus.subscribe("terminal.*.status")
        with _delivery_seq_guard:
            self._delivery_loop = asyncio.get_running_loop()
            self._delivery_registry = registry
            self._prestart_wake_logged = False
        logger.info("InboxService started")

        try:
            while True:
                try:
                    event = await queue.get()
                    status_value = event["data"]["status"]
                    if status_value in (
                        TerminalStatus.IDLE.value,
                        TerminalStatus.COMPLETED.value,
                    ):
                        terminal_id = terminal_id_from_topic(event["topic"])
                        await asyncio.to_thread(
                            self.deliver_pending, terminal_id, registry=registry
                        )
                except Exception as e:
                    logger.error(f"Error in InboxService: {e}")
        finally:
            with _delivery_seq_guard:
                self._delivery_loop = None
                self._delivery_registry = None
                self._posted_delivery_wakes.clear()
            tasks = list(self._delivery_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._delivery_tasks.clear()

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
        # F339: skip delivery for terminals already abandoned as ghosts.
        if self._f339_is_abandoned(terminal_id):
            return

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
            if not metadata:
                # F339: terminal row absent — record streak and bail if abandoned.
                outcome = self._f339_record_not_found(terminal_id, "no_terminal_metadata")
                if outcome.reason == "abandoned_no_terminal":
                    return
                return
            if metadata.get("recovery_state") not in (None, "rebound"):
                return
            # F339: terminal found — reset ghost-terminal streak.
            self._f339_reset_not_found(terminal_id)
            legacy_test_seam = begin_delivery_attempt is not _PRODUCTION_BEGIN_DELIVERY_ATTEMPT
            routed_generation: int | None = None
            provider = None
            if not legacy_test_seam:
                from cli_agent_orchestrator.services.mailbox_service import (
                    digest_stale_pending_for_terminal,
                )

                try:
                    _, routed_generation = digest_stale_pending_for_terminal(
                        terminal_id, include_generation=True
                    )
                except OperationalError as exc:
                    error_detail = str(exc).lower()
                    if "locked" not in error_detail and "busy" not in error_detail:
                        raise
                    # Digesting is the generation fence.  If its transaction
                    # cannot open, leave every row pending for the next wake;
                    # proceeding could expose a stale row to normal delivery.
                    return
                legacy_parked = get_owned_legacy_parked_messages(terminal_id)
                if legacy_parked:
                    if provider is None:
                        provider = provider_manager.get_provider(terminal_id)
                    from cli_agent_orchestrator.backends.registry import get_backend
                    from cli_agent_orchestrator.services.receiver_state_view import native_probe
                    from cli_agent_orchestrator.services.seam_activation import (
                        receiver_state_active,
                    )

                    if (
                        receiver_state_active("delivery.park_identity_probe")
                        and get_backend().supports_event_inbox()
                    ):
                        native_result = native_probe(terminal_id, status_monitor)
                        if native_result is None:
                            return
                        probe_meta = native_result.meta
                    else:
                        probe_result = status_monitor.probe_screen_status(terminal_id)
                        probe_meta = (
                            probe_result.meta if hasattr(probe_result, "meta") else probe_result[1]
                        )
                    if (
                        receiver_state_active("delivery.park_identity_probe")
                        and not get_backend().supports_event_inbox()
                    ):
                        view = status_monitor.receiver_state_store.snapshot_view(
                            (
                                terminal_id,
                                int(metadata["lifecycle_generation"]),
                                str(metadata["tmux_window"]),
                            ),
                            require_fresh=True,
                            max_age_s=2.0,
                            recovery_state=metadata.get("recovery_state"),
                            token=getattr(probe_result, "fresh_token", None),
                        )
                        if view is None or view.probe_evidence is None:
                            return
                        probe_meta = view.probe_evidence.to_legacy_dict()
                    if isinstance(probe_meta, dict):
                        safety = self._inject_safe(terminal_id, provider, probe_meta)
                        identity_failure = (
                            probe_meta.get("identity_proof_failure")
                            if isinstance(probe_meta, dict)
                            else None
                        )
                        if safety.reason == "identity_unverified" and isinstance(
                            identity_failure, str
                        ):
                            self._record_identity_authority_failure(
                                terminal_id,
                                legacy_parked,
                                metadata,
                                identity_failure,
                                routed_generation=routed_generation,
                            )
            with _delivery_seq_guard:
                if _delivery_wake_seq.get(terminal_id, 0) > captured_wake:
                    return
            limit = num_messages if num_messages > 0 else 100
            excluded: set[int] = set()
            scanned: set[int] = set()
            # Classify protected sets before the SQL LIMIT/grouping seam. This
            # deliberately scans beyond any number of D2-only heads.
            while not legacy_test_seam:
                page = get_pending_messages(
                    terminal_id, limit=100, excluded_message_ids=excluded | scanned
                )
                if not page:
                    break
                first = page[0]
                if first.id in excluded or first.id in scanned:
                    break
                first_attempts = list_message_attempts([first.id])
                protected_attempt = next(
                    (
                        item
                        for item in reversed(first_attempts)
                        if item.get("outcome") == "ambiguous"
                        and item.get("reason") == "confirmation_timeout"
                    ),
                    None,
                )
                if protected_attempt is not None:
                    durable_ids = list_attempt_member_ids(protected_attempt["attempt_uuid"])
                    group = get_pending_messages_by_ids(terminal_id, durable_ids)
                else:
                    _, first_group = next(
                        groupby(
                            page,
                            key=lambda item: (
                                item.sender_id,
                                item.orchestration_type,
                                bool(getattr(item, "park_warm", False)),
                            ),
                        )
                    )
                    group = list(first_group)
                if not group:
                    scanned.add(first.id)
                    continue
                state, detail = self._handle_wpm1_gate(
                    terminal_id,
                    group,
                    metadata,
                    provider,
                    first.sender_id,
                    first.orchestration_type,
                    park_warm=bool(getattr(first, "park_warm", False)),
                )
                ids = {item.id for item in group}
                if state == "skip_d2_only":
                    detail_map = detail if isinstance(detail, dict) else {}
                    member_ids = set(detail_map.get("member_ids") or ids)
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
                    terminal_id, limit=limit, excluded_message_ids=excluded
                )
            if not messages:
                return

            # --- WP-MAILBOX-CHANNEL: pull-mode gate (D6) ---
            # If the supervisor.mailbox_pull flag is on AND this terminal is the
            # current supervisor mailbox incarnation, skip the push entirely.
            # Rows stay PENDING; the supervisor drains them via list_messages/ack.
            from cli_agent_orchestrator.services.mailbox_service import (
                is_supervisor_mailbox_pull_terminal,
            )

            if is_supervisor_mailbox_pull_terminal(terminal_id):
                # WP-W2M-PUSH-BRIDGE: attempt teammate-push notification (best-effort).
                from cli_agent_orchestrator.services.teammate_push_service import (
                    _should_teammate_push,
                    attempt_teammate_push,
                )

                if _should_teammate_push(terminal_id):
                    try:
                        attempt_teammate_push(terminal_id, messages)
                        # fx168 FIX-4: Removed dead D9 doorbell call. deliver_pending
                        # holds delivery_lock here; ring_supervisor_doorbell's G1 gate
                        # tries the same non-reentrant lock → always "skipped_gate".
                        # The F136 runner's doorbell in _f136_post_delivery (armed by
                        # FIX-1's request_delivery signal) is the correct path.
                    except Exception as _push_exc:
                        logger.debug(f"teammate_push side-effect failed: {_push_exc}")
                return
            # --- end WP-MAILBOX-CHANNEL gate ---

            # Deliver in contiguous runs of the same sender and orchestration mode.
            # With the default num_messages=1 this is a single run; when draining
            # all pending messages (num_messages=0) a batch can span multiple groups,
            # so each run is sent separately to keep attribution and shaping correct.
            sent_count = 0
            for (sender_id, orchestration_type, park_warm), group in groupby(
                messages,
                key=lambda m: (
                    m.sender_id,
                    m.orchestration_type,
                    bool(getattr(m, "park_warm", False)),
                ),
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
                        terminal_id,
                        batch,
                        metadata,
                        provider,
                        sender_id,
                        orchestration_type,
                        park_warm=park_warm,
                        observe_binding_staleness=False,
                    )
                    if gate_state == "stop":
                        return
                    if gate_state == "skip_d2_only":
                        continue
                    admission_snapshot = None
                    wpm1_retry_pre_paste = bool(
                        isinstance(gate_evidence, dict)
                        and gate_evidence.get("_wpm1_retry_pre_paste") is True
                    )
                    admission_kind = "corrective" if gate_state == "inject" else "ordinary"
                    eager_eligible = False
                    if gate_state == "normal":
                        if legacy_test_seam and (
                            admission_status := receiver_state_view.snapshot_view(
                                "delivery.admission_status",
                                terminal_id,
                                max_age_s=5.0,
                                none_behavior="none",
                                monitor=status_monitor,
                            )
                        ) in (None, TerminalStatus.WAITING_USER_ANSWER):
                            return
                        if not legacy_test_seam:
                            try:
                                admission_snapshot = status_monitor.get_boundary_observation(
                                    terminal_id
                                )
                            except Exception:
                                return
                        if isinstance(getattr(admission_snapshot, "status", None), TerminalStatus):
                            status = receiver_state_view.view_from_legacy(
                                "delivery.admission_status",
                                terminal_id,
                                admission_snapshot.status,
                                max_age_s=5.0,
                                none_behavior="none",
                                monitor=status_monitor,
                            )
                            if status is None:
                                return
                        else:
                            admission_snapshot = None
                            status = receiver_state_view.snapshot_view(
                                "delivery.admission_status",
                                terminal_id,
                                max_age_s=5.0,
                                none_behavior="none",
                                monitor=status_monitor,
                            )
                            if status is None:
                                return
                            if metadata.get("provider") == "claude_code" and status not in {
                                TerminalStatus.IDLE,
                                TerminalStatus.COMPLETED,
                            }:
                                return
                        if metadata.get("provider") == "claude_code" and status not in {
                            TerminalStatus.IDLE,
                            TerminalStatus.COMPLETED,
                        }:
                            overlap = list_overlapping_attempts(message_ids)
                            if all(
                                item.get("outcome") == "deferred"
                                and item.get("reason") in {"delivery_deferred", "input_blocked"}
                                for item in overlap
                            ):
                                if provider is None:
                                    provider = provider_manager.get_provider(terminal_id)
                                if provider is not None:
                                    if provider.read_composer_draft_state() != "empty":
                                        return
                                    admission_kind = "s4_initial"
                        if status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
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
                                capabilities = (
                                    provider.capabilities if provider is not None else None
                                )
                                eager_eligible = bool(
                                    capabilities is not None
                                    and capabilities.accepts_input_while_processing
                                )
                            if not eager_eligible:
                                return
                    ambiguous_count = count_ambiguous_attempts(message_ids)
                    resolution = resolve_session_transcript(metadata)
                    exact_attempts = (
                        list_message_attempts(message_ids)
                        if legacy_test_seam
                        else self._exact_batch_attempts(message_ids)
                    )
                    successor_source: str | None = None
                    persisted_evidence = (
                        dict(gate_evidence)
                        if gate_state == "inject" and isinstance(gate_evidence, dict)
                        else transcript_ref(resolution)
                    )
                    opener_ref = _opener_transcript_ref(persisted_evidence)
                    successor_plans: list[SuccessorLookupPlan] = []
                    carried_plans = persisted_evidence.pop("_successor_lookup_plans", ())
                    if isinstance(carried_plans, tuple) and all(
                        isinstance(item, SuccessorLookupPlan) for item in carried_plans
                    ):
                        successor_plans.extend(carried_plans)
                    if gate_state == "normal":
                        # A hit wins across the entire exact-batch history. An
                        # unresolved older row must not hide a later hit or the
                        # durable ambiguity cap.
                        prior_lookups: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
                        for prior in exact_attempts:
                            if prior.get("outcome") is None:
                                continue
                            try:
                                prior_evidence = json.loads(prior.get("evidence") or "{}")
                            except (TypeError, json.JSONDecodeError):
                                prior_evidence = {}
                            if not isinstance(prior_evidence, dict):
                                prior_evidence = {}
                            evidence_snapshot = copy.deepcopy(prior_evidence)
                            result, lookup_evidence = _wpm2_lookup(
                                metadata,
                                prior["payload_hash"],
                                prior.get("started_at"),
                                prior_evidence,
                            )
                            successor_plans.append(
                                _successor_lookup_plan(
                                    prior,
                                    evidence_snapshot,
                                    result,
                                    lookup_evidence,
                                    metadata,
                                )
                            )
                            prior_lookups.append((prior, result, prior_evidence))
                            if result != "hit":
                                continue
                            won = _confirmed_settlement(
                                lambda: confirm_batch_from_prior_attempt(
                                    message_ids,
                                    prior["attempt_uuid"],
                                    on_confirmed=lambda: self._commit_watchdog_ops(
                                        terminal_id,
                                        sender_id,
                                        orchestration_type,
                                        metadata,
                                        park_warm,
                                    ),
                                )
                            )
                            if not won:
                                return
                            logger.info(
                                "Deduplicated delivery for terminal %s using attempt %s",
                                terminal_id,
                                prior["attempt_uuid"],
                            )
                            return

                        stale_resolution = self._resolve_stale_binding_prior_hits(
                            terminal_id, metadata, prior_lookups
                        )
                        if stale_resolution is not None:
                            state, stale_prior, _, candidate = stale_resolution
                            if state != "hit" or stale_prior is None:
                                return
                            won = _confirmed_settlement(
                                lambda: confirm_batch_from_prior_attempt(
                                    message_ids,
                                    stale_prior["attempt_uuid"],
                                    on_confirmed=lambda: self._commit_watchdog_ops(
                                        terminal_id,
                                        sender_id,
                                        orchestration_type,
                                        metadata,
                                        park_warm,
                                    ),
                                ),
                            )
                            if not won:
                                return
                            logger.info(
                                "Deduplicated delivery for terminal %s using %s authority",
                                terminal_id,
                                candidate or "refreshed binding",
                            )
                            return

                        ambiguous = [
                            prior
                            for prior in exact_attempts
                            if prior.get("outcome") == "ambiguous"
                            and prior.get("reason") == "confirmation_timeout"
                        ]
                        eligible_prior = None
                        post_paste_successor = False
                        for prior in reversed(ambiguous):
                            successors = [
                                item
                                for item in exact_attempts
                                if item.get("prior_attempt_uuid") == prior["attempt_uuid"]
                            ]
                            if all(attempt_proven_pre_paste(item) for item in successors):
                                eligible_prior = prior
                                break
                            post_paste_successor = True
                        facts = AuthorizationFacts(
                            prior_ambiguous_eligible=ObservedFact(
                                eligible_prior is not None and not wpm1_retry_pre_paste,
                                (
                                    eligible_prior.get("attempt_uuid")
                                    if eligible_prior and not wpm1_retry_pre_paste
                                    else None
                                ),
                            ),
                            prior_batch_hit=ObservedFact(False),
                            post_paste_successor_exists=post_paste_successor,
                            receiver_alive=bool(metadata),
                            composer_empty=False,
                        )
                        decision = run_post_auth_engine(
                            facts,
                            ambiguous_count=ambiguous_count,
                            exhausted_boundary_count=0,
                        )
                        if decision.kind == "stop":
                            if decision.evidence.get("reason") == "attempt_cap":
                                inferred = (
                                    find_inferred_delivery_evidence(message_ids[0], terminal_id)
                                    if len(message_ids) == 1
                                    else None
                                )
                                if inferred is not None:
                                    cap_evidence: dict[str, Any] = inferred
                                    won = _confirmed_settlement(
                                        lambda: transition_pending_to_inferred_delivered(
                                            message_ids[0],
                                            cap_evidence,
                                            on_confirmed=lambda: self._commit_watchdog_ops(
                                                terminal_id,
                                                sender_id,
                                                orchestration_type,
                                                metadata,
                                                park_warm,
                                            ),
                                        )
                                    )
                                    if won:
                                        self._evict_defer_state(batch)
                                        return
                                if transition_pending_to_delivery_failed(message_ids):
                                    self._notify_delivery_failed(terminal_id, message_ids)
                                logger.warning(
                                    "Delivery ambiguity cap reached for terminal %s messages %s",
                                    terminal_id,
                                    message_ids,
                                )
                            return
                        if decision.kind == "suppress":
                            return
                        if decision.kind == "tagged_replay":
                            decision_source = decision.evidence["prior_attempt_uuid"]
                            assert isinstance(decision_source, str)
                            successor_source = decision_source
                            admission_kind = "tagged_replay"
                            persisted_evidence["redelivery_tag"] = decision.evidence[
                                "redelivery_tag"
                            ]
                    if (
                        legacy_test_seam
                        and gate_state == "normal"
                        and (
                            admission_status := receiver_state_view.snapshot_view(
                                "delivery.admission_status",
                                terminal_id,
                                max_age_s=5.0,
                                none_behavior="none",
                                monitor=status_monitor,
                            )
                        )
                        in (None, TerminalStatus.WAITING_USER_ANSWER)
                    ):
                        _defer_messages(terminal_id, messages[sent_count:])
                        return
                    if gate_state == "inject":
                        source = persisted_evidence.pop("_wpm1_prior_attempt_uuid", None)
                        exhausted_count = 0
                        for item in exact_attempts:
                            try:
                                item_evidence = json.loads(item.get("evidence") or "{}")
                            except (TypeError, json.JSONDecodeError):
                                item_evidence = {}
                            if isinstance(item_evidence, dict) and item_evidence.get(
                                "boundary_exhausted_at"
                            ):
                                exhausted_count += 1
                        decision = run_post_auth_engine(
                            AuthorizationFacts(
                                prior_ambiguous_eligible=ObservedFact(
                                    isinstance(source, str),
                                    source if isinstance(source, str) else None,
                                ),
                                prior_batch_hit=ObservedFact(False),
                                post_paste_successor_exists=False,
                                receiver_alive=bool(metadata),
                                composer_empty=True,
                                binding_authority=True,
                                boundary_observation=persisted_evidence.get("boundary_authorized"),
                                continuity_cursor=persisted_evidence.get("last_observed_ref"),
                            ),
                            ambiguous_count=ambiguous_count,
                            exhausted_boundary_count=exhausted_count,
                        )
                        if decision.kind != "inject":
                            return
                        decision_source = decision.evidence["prior_attempt_uuid"]
                        assert isinstance(decision_source, str)
                        successor_source = decision_source
                        persisted_evidence["redelivery_tag"] = decision.evidence["redelivery_tag"]
                    shape_type = (
                        None
                        if registry is None and orchestration_type == OrchestrationType.SEND_MESSAGE
                        else orchestration_type
                    )
                    base_prepared = terminal_service.prepare_input(
                        terminal_id, combined, shape_type
                    )
                    wire_prepared = (
                        f"{_redelivery_tag(successor_source)}\n{base_prepared}"
                        if successor_source is not None
                        else base_prepared
                    )
                    challenge_sha256 = None
                    if len(batch) == 1:
                        wire_prepared, challenge_sha256 = _wire_with_attempt_challenge(
                            wire_prepared,
                            sender_id,
                            batch[0].id,
                        )
                    digest = wire_hash(wire_prepared)
                    normalized_fingerprint = normalized_confirmation_fingerprint(wire_prepared)
                    if normalized_fingerprint is not None:
                        persisted_evidence["normalized_payload_hash"] = normalized_fingerprint[0]
                        persisted_evidence["normalized_payload_length"] = normalized_fingerprint[1]
                    provider_name = metadata.get("provider", "unknown")
                    proof = make_admission_proof(admission_kind, message_ids, successor_source)
                    if not legacy_test_seam and list_delivering_attempts_for_terminal(terminal_id):
                        return
                    if not legacy_test_seam:
                        if provider is None:
                            provider = provider_manager.get_provider(terminal_id)
                        from cli_agent_orchestrator.backends.registry import get_backend
                        from cli_agent_orchestrator.services.receiver_state_view import native_probe
                        from cli_agent_orchestrator.services.seam_activation import (
                            receiver_state_active,
                        )

                        if (
                            receiver_state_active("delivery.fresh_probe")
                            and get_backend().supports_event_inbox()
                        ):
                            native_result = native_probe(terminal_id, status_monitor)
                            if native_result is None:
                                return
                            probe_status, probe_meta = native_result.status, native_result.meta
                        else:
                            probe_result = status_monitor.probe_screen_status(terminal_id)
                            if hasattr(probe_result, "status"):
                                probe_status, probe_meta = probe_result.status, probe_result.meta
                            else:
                                probe_status, probe_meta = probe_result[0], probe_result[1]
                        if (
                            receiver_state_active("delivery.fresh_probe")
                            and not get_backend().supports_event_inbox()
                        ):
                            view = status_monitor.receiver_state_store.snapshot_view(
                                (
                                    terminal_id,
                                    int(metadata["lifecycle_generation"]),
                                    str(metadata["tmux_window"]),
                                ),
                                require_fresh=True,
                                max_age_s=2.0,
                                recovery_state=metadata.get("recovery_state"),
                                token=getattr(probe_result, "fresh_token", None),
                            )
                            if view is None or view.probe_evidence is None:
                                return
                            probe_status = view.latched_status
                            probe_meta = view.probe_evidence.to_legacy_dict()
                        safety = self._inject_safe(terminal_id, provider, probe_meta)
                        if safety.verdict == "veto":
                            identity_failure = (
                                probe_meta.get("identity_proof_failure")
                                if isinstance(probe_meta, dict)
                                else None
                            )
                            if safety.reason == "identity_unverified" and isinstance(
                                identity_failure, str
                            ):
                                self._record_identity_authority_failure(
                                    terminal_id,
                                    batch,
                                    metadata,
                                    identity_failure,
                                    routed_generation=routed_generation,
                                )
                            with _delivery_seq_guard:
                                _delivery_wake_seq[terminal_id] = (
                                    _delivery_wake_seq.get(terminal_id, 0) + 1
                                )
                            logger.info(
                                "delivery_preopen_veto terminal=%s reason=%s gate=%s",
                                terminal_id,
                                safety.reason,
                                safety.gate_episode,
                            )
                            return
                        if probe_status not in {
                            TerminalStatus.IDLE,
                            TerminalStatus.COMPLETED,
                        } and not (eager_eligible and probe_status == TerminalStatus.PROCESSING):
                            return
                        if not isinstance(probe_meta, dict):
                            return
                        identity_failure = probe_meta.get("identity_proof_failure")
                        if isinstance(identity_failure, str):
                            self._record_identity_authority_failure(
                                terminal_id,
                                batch,
                                metadata,
                                identity_failure,
                                routed_generation=routed_generation,
                            )
                            return
                        persisted_evidence["screen_probe"] = probe_meta
                    if successor_source is not None and metadata.get("provider") == "claude_code":
                        corroboration = corroborate_claude_successor(tuple(successor_plans))
                        if corroboration.kind == "defer":
                            logger.info("redelivery_deferred_unquiescent terminal=%s", terminal_id)
                            return
                        if corroboration.kind == "confirmed":
                            assert corroboration.hit_attempt_uuid is not None
                            assert corroboration.hit_evidence is not None
                            from cli_agent_orchestrator.services.stalled_callback_watchdog import (
                                stalled_callback_watchdog,
                            )

                            with stalled_callback_watchdog.confirmed_settlement_guard():
                                if gate_state == "inject":
                                    settle_wpm1_terminal_batch(
                                        message_ids,
                                        MessageStatus.DELIVERED,
                                        terminal_id,
                                        confirmation_evidence=(
                                            corroboration.hit_attempt_uuid,
                                            corroboration.hit_evidence,
                                        ),
                                        on_confirmed=lambda: self._commit_watchdog_ops(
                                            terminal_id,
                                            sender_id,
                                            orchestration_type,
                                            metadata,
                                            park_warm,
                                        ),
                                    )
                                else:
                                    confirm_batch_from_prior_attempt(
                                        message_ids,
                                        corroboration.hit_attempt_uuid,
                                        on_confirmed=lambda: self._commit_watchdog_ops(
                                            terminal_id,
                                            sender_id,
                                            orchestration_type,
                                            metadata,
                                            park_warm,
                                        ),
                                    )
                            return
                    opener_args = (
                        batch,
                        terminal_id,
                        provider_name,
                        digest,
                        len(wire_prepared.encode()),
                        status_monitor.get_input_gen(terminal_id),
                        status_monitor.get_status_gen(terminal_id),
                    )
                    opener_kwargs: dict[str, str | None] = {
                        "evidence": json.dumps(persisted_evidence),
                        "prior_attempt_uuid": successor_source,
                        "challenge_sha256": challenge_sha256,
                    }

                    def evidence_at_submit(value):
                        if (
                            not isinstance(getattr(value, "status", None), TerminalStatus)
                            or not isinstance(getattr(value, "observation_epoch", None), str)
                            or type(getattr(value, "seq", None)) is not int
                        ):
                            return None
                        result = dict(persisted_evidence)
                        same_epoch = admission_kind != "s4_initial" or (
                            admission_snapshot is not None
                            and admission_snapshot.observation_epoch == value.observation_epoch
                        )
                        if not same_epoch:
                            return result
                        result["injection_completed_seq"] = {
                            "observation_epoch": value.observation_epoch,
                            "seq": value.seq,
                        }
                        if admission_snapshot is not None and (
                            admission_snapshot.status
                            not in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}
                            or value.status not in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}
                        ):
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
                            begin_delivery_attempt(*opener_args, **opener_kwargs)
                        )
                    else:
                        opened = begin_delivery_attempt_if_no_other_delivering(
                            *opener_args, admission_proof=proof, **opener_kwargs
                        )
                    if opened.kind != "opened":
                        logger.debug("WPM2 opener held %s: %s", terminal_id, opened.kind)
                        return
                    attempt_uuid = opened.attempt_uuid
                    assert attempt_uuid is not None

                    def settle_probable_delivery(
                        candidate_evidence: dict[str, Any],
                    ) -> tuple[bool, dict[str, Any]]:
                        return self._settle_probable_delivered(
                            attempt_uuid,
                            message_ids,
                            batch,
                            candidate_evidence,
                            opener_ref,
                            terminal_id,
                            sender_id,
                            orchestration_type,
                            metadata,
                            park_warm,
                        )

                    authority_lock = None
                    candidate_logical_id = getattr(batch[0], "logical_receiver_id", None)
                    logical_receiver_id = (
                        candidate_logical_id
                        if isinstance(candidate_logical_id, str)
                        and candidate_logical_id.startswith("mb_")
                        else None
                    )
                    if logical_receiver_id:
                        from cli_agent_orchestrator.services.mailbox_service import (
                            MailboxDomainError,
                            acquire_logical_sender_authority,
                        )

                        captured_authority = get_attempt_mailbox_authority(attempt_uuid)
                        if captured_authority is None:
                            settle_delivery_attempt(
                                attempt_uuid,
                                MessageStatus.PENDING,
                                "interrupted",
                                reason="mailbox_generation_changed",
                            )
                            self._reset_identity_authority(terminal_id)
                            return
                        try:
                            authority_lock = acquire_logical_sender_authority(
                                logical_receiver_id,
                                terminal_id,
                                captured_authority["generation"],
                            )
                        except MailboxDomainError:
                            settle_delivery_attempt(
                                attempt_uuid,
                                MessageStatus.PENDING,
                                "interrupted",
                                reason="mailbox_authority_timeout",
                            )
                            self._reset_identity_authority(terminal_id)
                            with _delivery_seq_guard:
                                _delivery_wake_seq[terminal_id] = (
                                    _delivery_wake_seq.get(terminal_id, 0) + 1
                                )
                            return
                        if authority_lock is None:
                            settle_delivery_attempt(
                                attempt_uuid,
                                MessageStatus.PENDING,
                                "interrupted",
                                reason="mailbox_generation_changed",
                            )
                            self._reset_identity_authority(terminal_id)
                            successor_id = get_current_mailbox_terminal(logical_receiver_id)
                            with _delivery_seq_guard:
                                wake_id = successor_id or terminal_id
                                _delivery_wake_seq[wake_id] = _delivery_wake_seq.get(wake_id, 0) + 1
                            if successor_id and successor_id != terminal_id:
                                self.deliver_pending(successor_id, registry=registry)
                            return

                    def submitted(value):
                        nonlocal submit_observation, submit_evidence
                        submit_observation = value
                        submit_evidence = evidence_at_submit(value)

                    try:
                        send_kwargs = {
                            "defer_on_dialog": True,
                            "registry": registry,
                            "sender_id": sender_id,
                            "orchestration_type": shape_type,
                            "original_message": combined,
                        }
                        if not legacy_test_seam:
                            send_kwargs["on_submitted"] = submitted
                        try:
                            submit_observation = terminal_service.send_prepared_input(
                                terminal_id, wire_prepared, **send_kwargs
                            )
                        finally:
                            if authority_lock is not None:
                                authority_lock.release()
                                authority_lock = None
                        self._reset_identity_authority(terminal_id)
                        if (
                            not isinstance(
                                getattr(submit_observation, "status", None), TerminalStatus
                            )
                            or not isinstance(
                                getattr(submit_observation, "observation_epoch", None), str
                            )
                            or type(getattr(submit_observation, "seq", None)) is not int
                        ):
                            submit_observation = None
                            submit_evidence = None
                        else:
                            submit_evidence = evidence_at_submit(submit_observation)
                    except (DeliveryDeferredError, TerminalInputBlockedError):
                        if submit_observation is None:
                            raise
                        self._reset_identity_authority(terminal_id)
                        probable, classified_evidence = settle_probable_delivery(
                            submit_evidence or {}
                        )
                        if probable:
                            return
                        settle_delivery_attempt_proof_safe(
                            attempt_uuid,
                            classified_evidence,
                            status_monitor.get_status_gen(terminal_id),
                        )
                        return
                    except PaneIdentityMismatchError as exc:
                        evidence = dict(persisted_evidence)
                        evidence["identity_proof"] = exc.reason
                        mailbox_authority = get_attempt_mailbox_authority(attempt_uuid)
                        if mailbox_authority is not None:
                            evidence["mailbox_authority"] = mailbox_authority
                        settle_delivery_attempt_proof_safe(
                            attempt_uuid,
                            evidence,
                            status_monitor.get_status_gen(terminal_id),
                            outcome="ambiguous",
                            reason=f"pane_identity_mismatch:{exc.reason}",
                        )
                        self._record_identity_authority_failure(
                            terminal_id,
                            batch,
                            metadata,
                            exc.reason,
                            attempt_uuid=attempt_uuid,
                        )
                        return
                    except Exception:
                        if submit_observation is None and legacy_test_seam:
                            raise
                        self._reset_identity_authority(terminal_id)
                        probable, classified_evidence = settle_probable_delivery(
                            submit_evidence or dict(persisted_evidence)
                        )
                        if probable:
                            return
                        settle_delivery_attempt_proof_safe(
                            attempt_uuid,
                            classified_evidence,
                            status_monitor.get_status_gen(terminal_id),
                        )
                        return
                    trace = get_message_trace(batch[0].id)
                    current_attempt = next(
                        x for x in trace["attempts"] if x["attempt_uuid"] == attempt_uuid
                    )
                    outcome, evidence = confirm_delivery(
                        metadata,
                        digest,
                        current_attempt["started_at"],
                        current_attempt.get("evidence"),
                        timeout=_confirmation_timeout_seconds(),
                    )
                    if successor_source is not None:
                        evidence = {**current_attempt.get("evidence", {}), **evidence}
                    if submit_evidence is not None:
                        evidence.update(submit_evidence)
                    inferred = (
                        find_inferred_delivery_evidence(batch[0].id, terminal_id)
                        if len(batch) == 1 and outcome not in {"hit", "unverified"}
                        else None
                    )
                    if inferred is not None:
                        inferred = self._evidence_for_confirmed_attempt(terminal_id, inferred)
                        won = _confirmed_settlement(
                            lambda: settle_open_attempt_inferred_delivered(
                                attempt_uuid,
                                inferred,
                                on_confirmed=lambda: self._commit_watchdog_ops(
                                    terminal_id,
                                    sender_id,
                                    orchestration_type,
                                    metadata,
                                    park_warm,
                                ),
                            )
                        )
                        if won:
                            logger.info(
                                "Inferred delivery for terminal %s message %s from challenge reply",
                                terminal_id,
                                batch[0].id,
                            )
                            self._evict_defer_state(batch)
                            return
                    if outcome in {"hit", "unverified"}:
                        if outcome == "hit":
                            evidence = self._evidence_for_confirmed_attempt(terminal_id, evidence)
                        _confirmed_settlement(
                            lambda: settle_delivery_attempt(
                                attempt_uuid,
                                MessageStatus.DELIVERED,
                                "confirmed",
                                evidence=json.dumps(evidence),
                                settled_status_gen=status_monitor.get_status_gen(terminal_id),
                                on_confirmed=lambda: self._commit_watchdog_ops(
                                    terminal_id,
                                    sender_id,
                                    orchestration_type,
                                    metadata,
                                    park_warm,
                                ),
                            ),
                        )
                    else:
                        probable, evidence = settle_probable_delivery(evidence)
                        if probable:
                            return
                        settle_delivery_attempt(
                            attempt_uuid,
                            MessageStatus.PENDING,
                            "ambiguous",
                            reason="confirmation_timeout",
                            evidence=json.dumps(evidence),
                            settled_status_gen=status_monitor.get_status_gen(terminal_id),
                        )
                        with _delivery_seq_guard:
                            _delivery_wake_seq[terminal_id] = (
                                _delivery_wake_seq.get(terminal_id, 0) + 1
                            )
                        return
                    logger.info(f"Delivered {len(batch)} message(s) to terminal {terminal_id}")
                    self._evict_defer_state(batch)
                except DeliveryDeferredError:
                    self._record_delivery_deferred(terminal_id, batch)
                    if attempt_uuid:
                        self._reset_identity_authority(terminal_id)
                        if submit_evidence is not None:
                            probable, classified_evidence = settle_probable_delivery(
                                submit_evidence
                            )
                            if probable:
                                return
                            settle_delivery_attempt_proof_safe(
                                attempt_uuid,
                                classified_evidence,
                                status_monitor.get_status_gen(terminal_id),
                            )
                            return
                        else:
                            settle_delivery_attempt(
                                attempt_uuid,
                                MessageStatus.PENDING,
                                "deferred",
                                reason="delivery_deferred",
                            )
                    else:
                        _defer_messages(terminal_id, messages[sent_count:])
                    with _delivery_seq_guard:
                        _delivery_wake_seq[terminal_id] = _delivery_wake_seq.get(terminal_id, 0) + 1
                    return
                except TerminalInputBlockedError:
                    if attempt_uuid:
                        self._reset_identity_authority(terminal_id)
                        if submit_evidence is not None:
                            probable, classified_evidence = settle_probable_delivery(
                                submit_evidence
                            )
                            if probable:
                                return
                            settle_delivery_attempt_proof_safe(
                                attempt_uuid,
                                classified_evidence,
                                status_monitor.get_status_gen(terminal_id),
                            )
                            return
                        else:
                            settle_delivery_attempt(
                                attempt_uuid,
                                MessageStatus.PENDING,
                                "deferred",
                                reason="input_blocked",
                            )
                    else:
                        _defer_messages(terminal_id, messages[sent_count:])
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
                            probable, classified_evidence = settle_probable_delivery(
                                submit_evidence
                            )
                            if probable:
                                return
                            settle_delivery_attempt_proof_safe(
                                attempt_uuid,
                                classified_evidence,
                                status_monitor.get_status_gen(terminal_id),
                            )
                            return
                        else:
                            settle_delivery_attempt(
                                attempt_uuid,
                                MessageStatus.PENDING,
                                "interrupted",
                                reason="terminal_not_found",
                                error=str(e),
                            )
                    else:
                        for message in batch:
                            update_message_status(message.id, MessageStatus.PENDING)
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
                                attempt_uuid, MessageStatus.FAILED, "failed", error=str(e)
                            )
                        else:
                            probable, classified_evidence = settle_probable_delivery(
                                submit_evidence or {}
                            )
                            if probable:
                                return
                            result = settle_delivery_attempt_proof_safe(
                                attempt_uuid,
                                classified_evidence,
                                status_monitor.get_status_gen(terminal_id),
                            )
                            return
                    for message in batch:
                        logger.error(
                            f"Failed to deliver message {message.id} to {terminal_id}: {e}"
                        )
                        if not attempt_uuid:
                            update_message_status(message.id, MessageStatus.FAILED)
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
        # fx158 D1/D2: pull-mode pending-push reconciler (bypasses deliver_pending).
        self.reconcile_pull_mode_notifications()
        # WP-MAILBOX-CHANNEL: quarantine malformed mailbox rows on daemon heartbeat.
        from cli_agent_orchestrator.services.config_service import ConfigService
        from cli_agent_orchestrator.services.mailbox_service import (
            quarantine_malformed_mailbox_rows,
        )

        if ConfigService.get("supervisor.mailbox_pull"):
            from cli_agent_orchestrator.clients.database import MailboxModel as _MBModel
            from cli_agent_orchestrator.clients.database import SessionLocal as _SL

            with _SL() as _db:
                supervisor_mailboxes = _db.query(_MBModel).filter_by(role="supervisor").all()
                for mb in supervisor_mailboxes:
                    try:
                        quarantine_malformed_mailbox_rows(mb.id)
                    except Exception as e:
                        logger.debug(f"Mailbox quarantine sweep failed for {mb.id}: {e}")

    def reconcile_pull_mode_notifications(self) -> None:
        """fx158 D1: Push notifications for pull-mode supervisor mailboxes.

        Bypasses deliver_pending entirely — routes directly to the push path.
        D3 selection: mailbox-driven, cursor-aware, grace-windowed.
        D9: per-mailbox failure isolation.
        """
        import hashlib

        from cli_agent_orchestrator.clients.database import InboxModel as _InboxModel
        from cli_agent_orchestrator.clients.database import MailboxModel as _MBModel
        from cli_agent_orchestrator.clients.database import SessionLocal as _SL
        from cli_agent_orchestrator.clients.database import TerminalModel as _TModel
        from cli_agent_orchestrator.clients.database import (
            begin_delivery_attempt,
            settle_delivery_attempt,
        )
        from cli_agent_orchestrator.services.config_service import ConfigService
        from cli_agent_orchestrator.services.mailbox_service import (
            is_supervisor_mailbox_pull_terminal,
        )
        from cli_agent_orchestrator.services.teammate_push_service import (
            PushOutcome,
            _should_teammate_push,
            attempt_teammate_push_reported,
        )

        # D3 condition 1: flag must be on
        if not ConfigService.get("supervisor.mailbox_pull"):
            return

        cutoff = _utcnow() - timedelta(seconds=INBOX_RECONCILE_GRACE_SECONDS)

        with _SL() as db:
            supervisor_mailboxes = db.query(_MBModel).filter_by(role="supervisor").all()

        for mb in supervisor_mailboxes:
            try:
                # D3 condition 2: current_terminal_id non-empty and pull-mode
                if not mb.current_terminal_id:
                    continue
                if not is_supervisor_mailbox_pull_terminal(mb.current_terminal_id):
                    continue

                # D3 condition 3: live terminals row exists
                with _SL() as db:
                    terminal_row = (
                        db.query(_TModel).filter_by(id=mb.current_terminal_id).one_or_none()
                    )
                if terminal_row is None:
                    continue

                # D3 condition 5: teammate_push flag gate
                if not _should_teammate_push(mb.current_terminal_id):
                    # F162 D10: rate-limited WARN when unregistered
                    tid = mb.current_terminal_id
                    now_ts = time.monotonic()
                    last = _fx158_gate5_last_warn.get(tid)
                    if last is None or (now_ts - last) >= _FX158_GATE5_WARN_INTERVAL_S:
                        with _SL() as db:
                            pending_count = (
                                db.query(_InboxModel)
                                .filter(
                                    _InboxModel.logical_receiver_id == mb.id,
                                    _InboxModel.status == MessageStatus.PENDING.value,
                                    _InboxModel.id > mb.consumed_through_id,
                                )
                                .count()
                            )
                        if pending_count > 0:
                            logger.warning(
                                "fx158_gate5_unregistered terminal=%s pending=%d",
                                tid,
                                pending_count,
                            )
                            _fx158_gate5_last_warn[tid] = now_ts
                    continue

                # D3 condition 4: pending rows older than grace, above consumed_through_id
                # F165-a: copy all needed scalars INSIDE the session to avoid
                # DetachedInstanceError on deferred columns (logical_receiver_id).
                with _SL() as db:
                    pending_rows = (
                        db.query(_InboxModel)
                        .filter(
                            _InboxModel.logical_receiver_id == mb.id,
                            _InboxModel.status == MessageStatus.PENDING.value,
                            _InboxModel.id > mb.consumed_through_id,
                            _InboxModel.created_at < cutoff,
                        )
                        .order_by(_InboxModel.id)
                        .limit(100)
                        .all()
                    )
                    # Materialise scalars while session is open (deferred cols
                    # like logical_receiver_id trigger DetachedInstanceError
                    # after session close).
                    pending_scalars = [
                        {
                            "id": row.id,
                            "sender_id": row.sender_id,
                            "receiver_id": row.receiver_id,
                            "message": row.message,
                            "orchestration_type": row.orchestration_type,
                            "status": row.status,
                            "created_at": row.created_at,
                            "logical_receiver_id": getattr(row, "logical_receiver_id", None),
                        }
                        for row in pending_rows
                    ]

                if not pending_scalars:
                    continue

                # Convert to InboxMessage for the push function
                messages = [
                    InboxMessage(
                        id=s["id"],
                        sender_id=s["sender_id"],
                        receiver_id=s["receiver_id"],
                        message=s["message"],
                        orchestration_type=OrchestrationType(s["orchestration_type"]),
                        status=MessageStatus(s["status"]),
                        created_at=s["created_at"],
                        logical_receiver_id=s["logical_receiver_id"],
                    )
                    for s in pending_scalars
                ]

                # D4: call the reported form directly
                outcome: PushOutcome = attempt_teammate_push_reported(
                    mb.current_terminal_id, messages
                )

                # F168 D9: ring doorbell after reconciler push write.
                # F186: pass caller_holds_no_delivery_lock=True — the reconciler
                # does NOT hold delivery_lock, so G1 must be skipped to avoid the
                # systematic contention that fx168 FIX-4 identified at the primary site.
                if outcome.pushed and outcome.message_ids:
                    try:
                        from cli_agent_orchestrator.services.doorbell_service import (
                            ring_supervisor_doorbell,
                        )

                        max_id = max(outcome.message_ids)
                        ring_supervisor_doorbell(
                            mb.current_terminal_id,
                            max_id,
                            written_count=1,
                            caller_holds_no_delivery_lock=True,
                        )
                    except Exception:
                        pass

                # D5: instrumentation — record attempt row
                if outcome.reason == "pushed":
                    db_outcome = "push_written"
                elif outcome.reason in ("no_inbox_path", "write_failed"):
                    db_outcome = "push_failed"
                else:
                    db_outcome = "push_suppressed"

                # S2: deterministic payload hash from sorted message ids
                payload_hash = hashlib.sha256(
                    json.dumps(sorted(m.id for m in messages)).encode()
                ).hexdigest()

                try:
                    attempt_uuid = begin_delivery_attempt(
                        messages,
                        mb.current_terminal_id,
                        provider="reconciler",
                        payload_hash=payload_hash,
                        payload_length=len(messages),
                    )
                    settle_delivery_attempt(
                        attempt_uuid,
                        MessageStatus.PENDING,  # rows stay PENDING (pull-mode)
                        outcome=db_outcome,
                        reason=outcome.reason,
                    )
                except Exception as e:
                    logger.debug(f"fx158 instrumentation write failed for {mb.id}: {e}")

            except Exception as e:
                # D9: per-mailbox failure isolation.
                # F165-F1: distinguish transient errors (network/DB) from
                # programming errors (ORM detachment, type errors) that indicate
                # broken code and would silently kill every tick forever.
                from sqlalchemy.exc import InterfaceError as _SAInterfaceError

                _D9_TRANSIENT_TYPES = (OSError, OperationalError, TimeoutError, _SAInterfaceError)
                if isinstance(e, _D9_TRANSIENT_TYPES):
                    logger.warning(
                        "fx158_reconciler_transient mailbox=%s: %s",
                        mb.id,
                        e,
                    )
                else:
                    # Programming error — surface loudly so it is not invisible.
                    logger.error(
                        "fx158_reconciler_programming_error mailbox=%s: %s",
                        mb.id,
                        e,
                        exc_info=True,
                    )
                    # Record a durable marker so the failure is observable in
                    # delivery_attempts even if logs rotate.
                    try:
                        import uuid as _uuid

                        from cli_agent_orchestrator.clients.database import (
                            InboxDeliveryAttemptModel as _AttemptModel,
                        )
                        from cli_agent_orchestrator.clients.database import SessionLocal as _ErrSL

                        _now = _utcnow()
                        _err_row = _AttemptModel(
                            attempt_uuid=str(_uuid.uuid4()),
                            receiver_terminal_id=mb.current_terminal_id or "unknown",
                            provider="reconciler",
                            started_at=_now,
                            settled_at=_now,
                            outcome="programming_error",
                            reason=f"{type(e).__name__}: {e}"[:200],
                            payload_hash="error",
                            payload_length=0,
                            sender_id="system",
                            orchestration_type="reconciler_error",
                            evidence="{}",
                        )
                        with _ErrSL.begin() as _err_db:
                            _err_db.add(_err_row)
                    except Exception:
                        pass  # best-effort instrumentation

    def reconcile_pending_orphans(self) -> OrphanReconcileResult:
        """Settle one bounded batch of PENDING rows with absent receivers."""
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.services.mailbox_service import (
            get_mailbox_authority_lock,
        )

        for terminal_id in list_pending_receiver_ids_with_terminal():
            metadata = get_terminal_metadata(terminal_id)
            if metadata is None:
                continue
            try:
                liveness = get_backend().window_liveness(
                    metadata["tmux_session"], metadata["tmux_window"]
                )
            except Exception:
                liveness = "error"
            with self._gone_lock:
                if liveness != "gone":
                    self._gone_streaks.pop(terminal_id, None)
                    continue
                streak = self._gone_streaks.get(terminal_id, 0) + 1
                self._gone_streaks[terminal_id] = streak
            if streak < 3:
                continue
            captured_generation = metadata.get("lifecycle_generation")
            if type(captured_generation) is not int:
                with self._gone_lock:
                    self._gone_streaks.pop(terminal_id, None)
                continue
            delivery_lock = get_delivery_lock(terminal_id)
            if not delivery_lock.acquire(blocking=False):
                continue
            authority_lock = get_mailbox_authority_lock(metadata["tmux_session"], "supervisor")
            authority_acquired = False
            try:
                authority_acquired = authority_lock.acquire(blocking=False)
                if not authority_acquired:
                    continue
                try:
                    final_liveness = get_backend().window_liveness(
                        metadata["tmux_session"], metadata["tmux_window"]
                    )
                except Exception:
                    final_liveness = "error"
                if final_liveness not in {"gone", "live"}:
                    continue
                gone_result = settle_pending_receiver_gone_if_generation(
                    terminal_id, captured_generation
                )
                if gone_result.settled_count:
                    logger.info(
                        "P5 locked liveness reconciliation settled %d message(s) for %s",
                        gone_result.settled_count,
                        terminal_id,
                    )
            finally:
                with self._gone_lock:
                    self._gone_streaks.pop(terminal_id, None)
                if authority_acquired:
                    authority_lock.release()
                delivery_lock.release()
        result = settle_pending_orphan_messages()
        if result.busy_aborted:
            logger.warning("P5 orphan reconciliation aborted after bounded database contention")
        elif result.settled_count:
            logger.info(
                "P5 orphan reconciliation settled %d message(s), queued %d notice(s), "
                "logged-only %d batch(es)",
                result.settled_count,
                result.notification_count,
                result.logged_only_count,
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
                # F74: don't mark already-terminal messages as DELIVERY_FAILED
                statuses = self._message_statuses(message_ids)
                terminal_states = {
                    MessageStatus.DELIVERED.value,
                    MessageStatus.DELIVERY_FAILED.value,
                    MessageStatus.DIGESTED.value,
                }
                eligible = [mid for mid in message_ids if statuses.get(mid) not in terminal_states]
                if not eligible:
                    return
                recover_wpm2_stale_attempt(
                    attempt_uuid,
                    eligible,
                    MessageStatus.DELIVERY_FAILED,
                    "failed",
                    "receiver_gone",
                    {},
                )
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
                    metadata, attempt["payload_hash"], attempt.get("started_at"), evidence
                )
            if lookup == "hit":
                result = recover_wpm2_stale_attempt(
                    attempt_uuid,
                    message_ids,
                    MessageStatus.DELIVERED,
                    "confirmed",
                    "stale_recovery",
                    lookup_evidence,
                )
                if result == "settled":
                    self._commit_watchdog_ops(
                        terminal_id,
                        attempt["sender_id"],
                        OrchestrationType(attempt["orchestration_type"]),
                        metadata,
                        get_park_warm_for_message_ids(message_ids),
                    )
                return
            recovered_at = _utcnow().isoformat().replace("+00:00", "Z")
            recovery_evidence = {
                **lookup_evidence,
                "crash_recovery": {
                    "kind": "possibly_submitted_without_anchor",
                    "recovered_at": recovered_at,
                    "lookup_kind": lookup_evidence.get("kind", "transcript_unresolved"),
                },
            }
            # F74: before resurrecting to PENDING, verify messages are still
            # eligible (DELIVERING).  A message already at a terminal state
            # (DELIVERED, DELIVERY_FAILED, DIGESTED) must not be re-driven.
            statuses = self._message_statuses(message_ids)
            terminal_states = {
                MessageStatus.DELIVERED.value,
                MessageStatus.DELIVERY_FAILED.value,
                MessageStatus.DIGESTED.value,
            }
            if any(statuses.get(mid) in terminal_states for mid in message_ids):
                logger.info(
                    "F74: skipping resurrection to PENDING — messages %s already terminal",
                    [mid for mid in message_ids if statuses.get(mid) in terminal_states],
                )
                return
            # F44-T6: before resurrecting to PENDING, classify the persisted opener
            # evidence. If it proves the payload was executed, settle inferred-delivered
            # instead of re-driving the message. Falls through to resurrection when the
            # predicate is False or the inferred-delivered settle loses its CAS.
            opener_ref = _opener_transcript_ref(evidence)
            classified = _classify_probable_delivery(evidence, terminal_id, opener_ref)
            if is_probable_delivered(classified):
                settlement_evidence = self._evidence_for_confirmed_attempt(
                    terminal_id, {**classified, "kind": "execution_evidence"}
                )
                won = _confirmed_settlement(
                    lambda: settle_attempt_inferred_delivered_batch(
                        attempt_uuid,
                        sorted(message_ids),
                        settlement_evidence,
                        on_confirmed=lambda: self._commit_watchdog_ops(
                            terminal_id,
                            attempt["sender_id"],
                            OrchestrationType(attempt["orchestration_type"]),
                            metadata,
                            get_park_warm_for_message_ids(message_ids),
                        ),
                    )
                )
                if won:
                    logger.info(
                        "T6 recovery inferred_delivered %s receiver=%s",
                        sorted(message_ids),
                        terminal_id,
                    )
                    return
            recover_wpm2_stale_attempt(
                attempt_uuid,
                message_ids,
                MessageStatus.PENDING,
                "ambiguous",
                "confirmation_timeout",
                recovery_evidence,
            )
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
                self._recover_wpm2_attempt(
                    {
                        **attempt,
                        "receiver_terminal_id": message.receiver_id,
                        "message_ids": message_ids,
                    }
                )
                continue
            metadata = get_terminal_metadata(message.receiver_id)
            if not metadata:
                settle_delivery_attempt(
                    attempt_uuid, MessageStatus.FAILED, "failed", reason="receiver_metadata_gone"
                )
                continue
            try:
                from cli_agent_orchestrator.backends.registry import get_backend

                get_backend().get_history(
                    metadata["tmux_session"], metadata["tmux_window"], tail_lines=1
                )
            except Exception:
                settle_delivery_attempt(
                    attempt_uuid, MessageStatus.PENDING, "interrupted", reason="pane_unresolvable"
                )
                continue
            resolution = resolve_session_transcript(metadata)
            if resolution is None:
                settle_delivery_attempt(
                    attempt_uuid, MessageStatus.PENDING, "interrupted", reason="no_oracle"
                )
                continue
            path = getattr(resolution, "path", resolution)
            result, evidence = transcript_lookup(
                path, attempt["payload_hash"], attempt.get("started_at"), attempt.get("evidence")
            )
            evidence["resolution_kind"] = getattr(resolution, "resolution_kind", "exact_id")
            stale_note = getattr(resolution, "stale_note", None)
            if stale_note:
                evidence["binding_stale"] = stale_note
            if result == "hit":
                _confirmed_settlement(
                    lambda: settle_delivery_attempt(
                        attempt_uuid,
                        MessageStatus.DELIVERED,
                        "confirmed",
                        reason="startup_sweep",
                        evidence=json.dumps(evidence),
                        on_confirmed=lambda: self._commit_watchdog_ops(
                            message.receiver_id,
                            attempt["sender_id"],
                            OrchestrationType(attempt["orchestration_type"]),
                            metadata,
                            get_park_warm_for_message_ids(message_ids),
                        ),
                    ),
                )
            elif result == "absent":
                settle_delivery_attempt(
                    attempt_uuid,
                    MessageStatus.PENDING,
                    "interrupted",
                    reason="proven_absent",
                    evidence=json.dumps(evidence),
                )
            else:
                settle_delivery_attempt(
                    attempt_uuid,
                    MessageStatus.DELIVERY_FAILED,
                    "unresolved",
                    reason="continuity_uncertain",
                    evidence=json.dumps(evidence),
                )
                self._notify_delivery_failed(message.receiver_id, message_ids)


inbox_service = InboxService()
