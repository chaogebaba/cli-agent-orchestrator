"""What the SERVER did, recorded beside what it saw (AC4c, hook points 3, 6, 7).

A worker-truth row says what a producer observed.  A decision row says what the
server chose to do about it, and cites the ``event_id`` that justified the
choice.  That pairing is the whole of decision U9: a quirk should be
reconstructable from stored rows with one command, and "the server tore this
terminal down" is only reconstructable if the observation it acted on is named.

Three hook points, one per shape of decision:

* **hook 3 — ``delivery.attempt``**, one row per exit of each dispatch function.
  The blueprint is emphatic that the hook belongs at the FUNCTION exit and not at
  the ``verify_submission_after_send`` call, and the reason is #555: the paste
  can die before verify is ever reached.  ``preserve_draft_before_send`` raising
  "Composer state is unreadable" happens *above* the try block, so a hook at the
  verify call would record nothing at all for the exact failure that started this
  work package.  "``delivery.attempt`` appended at the verify call instead of the
  function exit" is a phase-1 mutant, and the draft-guard test kills it.
* **hook 6 — ``teardown.decided``**, at ``_claim_and_settle_deferred_failure``,
  carrying the settlement code.
* **hook 7 — ``teardown.intended``**, at the ``open_intent`` call in
  ``delete_terminal``, carrying the scope and TTL.  The liveness probe reads
  these rows back to decide whether a vanished pane is a ``teardown`` or a
  ``crash``; #571 is what happens when that distinction lives nowhere.

Legacy exception types are matched **by class name, never by import**.  The
``new-code-never-imports-legacy`` contract is not negotiable for a phase whose
point is to stop the new tree from being welded to the old one, and the names are
a stable enough surface for a classifier whose worst failure is labelling an
outcome ``error`` instead of ``deferred``.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, TypeVar

from cli_agent_orchestrator.adapters.truth import codex_rollout, legacy_egress
from cli_agent_orchestrator.adapters.truth.wiring import emit, producer_runtime
from cli_agent_orchestrator.core.events import (
    Confidence,
    DecisionKind,
    EventDraft,
    Producer,
)
from cli_agent_orchestrator.core.ids import new_ulid

__all__ = [
    "DELIVERY_OUTCOMES",
    "classify_outcome",
    "dispatch_attempt",
    "record_delivery_attempt",
    "record_teardown_decided",
    "record_teardown_intended",
]

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

#: The closed outcome vocabulary of ``delivery.attempt`` (blueprint AC4c).
DELIVERY_OUTCOMES = frozenset({"confirmed", "stuck", "composer_unreadable", "deferred", "error"})

#: A submit that never landed.  ``send_input`` re-raises it as a
#: ``DeliveryDeferredError``, so the cause chain is walked as well as the type.
_STUCK_NAMES = frozenset({"CodexSubmitStuckError"})

#: Retry-safe: the terminal is coherent and the inbox will redeliver.
_DEFERRED_NAMES = frozenset(
    {"DeliveryDeferredError", "DialogOpenError", "TerminalInputBlockedError"}
)

#: A dialog is open.  A subclass of ``DeliveryDeferredError``, and deliberately
#: NOT ``composer_unreadable``: the composer was read fine, it just is not free.
_DIALOG_NAMES = frozenset({"DialogOpenError"})

#: The #555 signature.  ``draft_guard`` raises a plain ``DeliveryDeferredError``
#: with this text rather than a dedicated class, so the message is one of the two
#: signals; the other is a ``draft_guard`` frame in the traceback.  Either is
#: enough.  A typed ``ComposerUnreadableError`` would retire both and is worth
#: raising in phase 2, when ``draft_guard`` is in scope for edits.
_COMPOSER_MARKER = "unreadable"
_COMPOSER_MODULES = ("draft_guard.py",)


def _class_names(exc: BaseException) -> set[str]:
    return {klass.__name__ for klass in type(exc).__mro__}


def _chain(exc: BaseException) -> list[BaseException]:
    """The exception and its ``__cause__``/``__context__`` chain, cycle-safe."""
    seen: list[BaseException] = []
    identities: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in identities:
        seen.append(current)
        identities.add(id(current))
        current = current.__cause__ or current.__context__
    return seen


def _from_draft_guard(exc: BaseException) -> bool:
    traceback = exc.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename
        if any(filename.endswith(module) for module in _COMPOSER_MODULES):
            return True
        traceback = traceback.tb_next
    return False


def classify_outcome(exc: BaseException | None) -> str:
    """Map a dispatch exit to one of :data:`DELIVERY_OUTCOMES`.

    Order matters.  ``stuck`` is checked first because ``send_input`` converts a
    ``CodexSubmitStuckError`` into a ``DeliveryDeferredError``, so by the time the
    exception reaches the function exit it looks deferred and only the cause
    chain still says otherwise.  Collapsing the two would erase the distinction
    between "the submit key was lost" and "we chose not to deliver yet", which is
    the distinction #555 turned on.
    """
    if exc is None:
        return "confirmed"

    chain = _chain(exc)
    names: set[str] = set()
    for link in chain:
        names |= _class_names(link)

    if names & _STUCK_NAMES:
        return "stuck"
    if names & _DEFERRED_NAMES:
        if not (names & _DIALOG_NAMES) and (
            _COMPOSER_MARKER in str(exc).lower() or any(_from_draft_guard(e) for e in chain)
        ):
            return "composer_unreadable"
        return "deferred"
    return "error"


def _detail(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    return f"{type(exc).__name__}: {exc}"


def record_delivery_attempt(
    terminal_id: str,
    *,
    carrier: str,
    exc: BaseException | None = None,
    msg_id: str | None = None,
) -> None:
    """Append ONE ``delivery.attempt`` row for one dispatch-function exit.

    ``evidence`` is the terminal's most recent ``status.legacy_published``
    ``event_id`` — the observation the server was acting on when it dispatched.
    On a ``confirmed`` outcome ``source_ref`` additionally names the rollout
    record that confirmed the submission, so this decision and the tailer's
    ``submission.confirmed`` row join on ``source_ref`` rather than on a
    timestamp window (r9 retired N3's stamping window).

    Never raises.
    """
    runtime = producer_runtime()
    if runtime is None:
        return
    try:
        outcome = classify_outcome(exc)
        source_ref: str | None = None
        if outcome == "confirmed":
            source_ref = codex_rollout.latest_submission_source_ref(terminal_id)
        emit(
            EventDraft(
                terminal_id=terminal_id,
                kind=DecisionKind.DELIVERY_ATTEMPT,
                producer=Producer.SERVER,
                confidence=Confidence.DERIVED,
                observed_at=runtime.clock.now(),
                decision=DecisionKind.DELIVERY_ATTEMPT,
                evidence=legacy_egress.last_published_event_id(terminal_id),
                msg_id=msg_id or new_ulid(),
                source_ref=source_ref,
                payload={"carrier": carrier, "outcome": outcome, "detail": _detail(exc)},
            )
        )
    except Exception:  # pragma: no cover - the never-break-the-dispatch rule
        logger.debug("delivery.attempt hook failed for %s", terminal_id, exc_info=True)


def dispatch_attempt(carrier: str) -> Callable[[F], F]:
    """Hook point 3 — wrap one dispatch function so every exit writes one row.

    A decorator rather than a pair of statements inside the body, for three
    reasons that all point the same way:

    1. ``send_prepared_input`` has no outer ``try``, so capturing its failures
       inline would mean re-indenting the whole function — a large diff in a
       phase whose acceptance criterion is that legacy files barely change.
    2. Every exit is covered, including the ones a hand-placed statement forgets:
       the early fixture return, and any exception raised above the inner try.
       That is precisely what makes the ``preserve_draft_before_send`` path
       observable.
    3. With ingestion off the wrapper delegates straight through with no
       ``try``/``except`` of its own, so the OFF path is a single function call
       and cannot alter exception identity, traceback or control flow.

    ``terminal_id`` is read from the first positional argument, which is the
    signature both dispatch functions share.
    """

    def decorate(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if producer_runtime() is None:
                return func(*args, **kwargs)
            terminal_id = args[0] if args else kwargs.get("terminal_id", "")
            try:
                result = func(*args, **kwargs)
            except BaseException as exc:
                if isinstance(exc, Exception):
                    record_delivery_attempt(str(terminal_id), carrier=carrier, exc=exc)
                raise
            record_delivery_attempt(str(terminal_id), carrier=carrier)
            return result

        return wrapper  # type: ignore[return-value]

    return decorate


def record_teardown_decided(terminal_id: str, code: str, detail: str = "") -> None:
    """Hook point 6 — ``_claim_and_settle_deferred_failure`` settled a failure."""
    runtime = producer_runtime()
    if runtime is None:
        return
    try:
        emit(
            EventDraft(
                terminal_id=terminal_id,
                kind=DecisionKind.TEARDOWN_DECIDED,
                producer=Producer.SERVER,
                confidence=Confidence.DERIVED,
                observed_at=runtime.clock.now(),
                decision=DecisionKind.TEARDOWN_DECIDED,
                evidence=legacy_egress.last_published_event_id(terminal_id),
                payload={"code": code, "detail": detail},
            )
        )
    except Exception:  # pragma: no cover - the never-break-the-teardown rule
        logger.debug("teardown.decided hook failed for %s", terminal_id, exc_info=True)


def record_teardown_intended(
    terminal_id: str,
    *,
    scope_kind: str,
    scope_key: str,
    ttl_s: float,
    requested_by: str | None = None,
) -> None:
    """Hook point 7 — a teardown intent was opened for this terminal.

    The liveness probe reads these rows back within their TTL to label a
    ``process.exited`` as ``teardown`` rather than ``crash``.  The row is written
    whether or not the durable DB intent succeeded: ``delete_terminal`` continues
    on the in-process mark when the DB write fails, and the truth log has to
    describe what the server INTENDED, which is what the probe needs to know.
    """
    runtime = producer_runtime()
    if runtime is None:
        return
    try:
        emit(
            EventDraft(
                terminal_id=terminal_id,
                kind=DecisionKind.TEARDOWN_INTENDED,
                producer=Producer.SERVER,
                confidence=Confidence.DERIVED,
                observed_at=runtime.clock.now(),
                decision=DecisionKind.TEARDOWN_INTENDED,
                evidence=legacy_egress.last_published_event_id(terminal_id),
                payload={
                    "scope_kind": scope_kind,
                    "scope_key": scope_key,
                    "ttl_s": float(ttl_s),
                    "requested_by": requested_by,
                },
            )
        )
    except Exception:  # pragma: no cover - the never-break-the-teardown rule
        logger.debug("teardown.intended hook failed for %s", terminal_id, exc_info=True)
