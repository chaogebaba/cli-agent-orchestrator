"""A1's carrier: the ordinal, the emitter count, the refusals, the sender.

AC-3b cases 16 through 19, plus the mutant §14 says to write FIRST — a wake line
without an advancing ``wake=<w>``, whose every re-emission after the first is
swallowed by the transport's per-sender content window with the write reporting
success.  That is a silent failure on the path the amendment exists to make
reliable, so :class:`RecordingCarrier` reproduces the window rather than
pretending it away: a byte-identical payload from the same sender is DROPPED and
reported as a success, exactly as ``write_to_socket`` does.

Against the REAL store, because three of these cases read a column — ``wake_count``
is the durable half of I3's enforcement and the transport's window is the half a
server bounce clears, so a test that counted emissions in a double would be
testing the half that does not survive a restart.

The carriers are doubles, because what is under test is the DISPATCH and the
classification, and a real socket would make every case a liveness test of the
harness.  Case 17's emitter count is taken at the seams the doubles stand in for
— never from a rendered transcript, which #613 showed can report zero emitters on
a seat where emitters had in fact fired.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS, FakeClock

import pytest

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
from cli_agent_orchestrator.app.delivery.tick import DeliveryTick
from cli_agent_orchestrator.app.delivery.wake import WakeService
from cli_agent_orchestrator.core.delivery import (
    UNVERIFIED_STREAK_LEASES,
    AttemptOutcome,
    DeadReason,
    EnqueueDraft,
    InjectionResult,
    MsgState,
    QueueMode,
    ReceiverResolution,
    SwitchPosition,
    WakeEmission,
)
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.timing import (
    DELIVERY_BACKOFF_S,
    DELIVERY_LEASE_S,
    DELIVERY_MAX_LIFETIME_S,
)

SEAT = "mb_supervisor"
WORKER = "mb_worker"


# --------------------------------------------------------------------- doubles


@dataclass
class Emission:
    """One native write, recorded at the seam (§A1.6, case 17)."""

    terminal_id: str
    line: str
    sender_key: str
    sender_name: str
    msg_id: str


class RecordingCarrier:
    """A ``SeatCarrier`` that reproduces the transport's dedupe window.

    ``write_to_socket`` short-circuits on a per-sender window of the twenty most
    recent CONTENT hashes and returns SUCCESS without writing.  Reproducing that
    here is the whole point of the ordinal mutant: with the window modelled, a
    line that does not vary per lease is silently dropped, and a test that
    counted ``emit`` calls instead of writes would pass against the mutant.
    """

    def __init__(self, window: int = 20) -> None:
        self.writes: list[Emission] = []
        self.calls: list[Emission] = []
        self.reason: str | None = None
        self.verified: bool = True
        self.annotations: tuple[str, ...] = ()
        self._windows: dict[str, list[str]] = {}
        self._window = window

    def emit(
        self,
        *,
        terminal_id: str,
        line: str,
        sender_key: str,
        sender_name: str,
        msg_id: str,
    ) -> WakeEmission:
        record = Emission(terminal_id, line, sender_key, sender_name, msg_id)
        self.calls.append(record)
        if self.reason is not None:
            return WakeEmission(reason=self.reason, annotations=self.annotations)
        digest = hashlib.sha256(line.encode()).hexdigest()
        window = self._windows.setdefault(sender_key, [])
        if digest in window:
            # Success without a write: the failure mode the ordinal exists to
            # prevent, reported exactly as the transport reports it.
            return WakeEmission(reason=None, verified=self.verified)
        window.append(digest)
        del window[: -self._window]
        self.writes.append(record)
        return WakeEmission(reason=None, verified=self.verified, annotations=self.annotations)


class RecordingInjector:
    """A ``PaneInjector`` that counts pane writes at the backend seam."""

    def __init__(self) -> None:
        self.pastes: list[tuple[str, str]] = []
        self.outcome = AttemptOutcome.DELIVERED

    def inject(self, *, terminal_id: str, line: str) -> InjectionResult:
        self.pastes.append((terminal_id, line))
        return InjectionResult(outcome=self.outcome, detail="pane")


@dataclass
class FakeDirectory:
    """A ``ReceiverDirectory`` whose answers a test sets."""

    roles: dict[str, bool] = field(default_factory=dict)
    terminals: dict[str, str] = field(default_factory=dict)
    panes: dict[str, bool] = field(default_factory=dict)

    def resolve(self, receiver_id: str) -> ReceiverResolution:
        terminal = self.terminals.get(receiver_id, f"term-{receiver_id}")
        return ReceiverResolution(
            receiver_id=receiver_id,
            terminal_id=terminal,
            is_supervisor=self.roles.get(receiver_id, receiver_id == SEAT),
            pane_present=self.panes.get(receiver_id, True),
            display_name=receiver_id,
        )


class RecordingFindings:
    """A ``FindingStore`` that dedupes the way the real one does."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict[str, object]] = {}

    def record(
        self,
        code: FindingCode,
        *,
        terminal_id: str = "",
        dedupe_key: str = "",
        detail: str = "",
        sample_event_id: str | None = None,
    ) -> object:
        key = (code.value, terminal_id, dedupe_key)
        row = self.rows.setdefault(
            key, {"code": code, "terminal_id": terminal_id, "detail": detail, "count": 0}
        )
        row["count"] = int(row["count"]) + 1  # type: ignore[arg-type]
        return row

    def of(self, code: FindingCode) -> list[dict[str, object]]:
        return [row for key, row in self.rows.items() if key[0] == code.value]

    def list_findings(
        self, *, state: str | None = None, code: FindingCode | None = None
    ) -> list[object]:
        return []


# -------------------------------------------------------------------- fixtures


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[ConnectionPool]:
    result, connection_pool = migrate(tmp_path / "queue.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok, result
    assert connection_pool is not None
    yield connection_pool
    connection_pool.close_all()


@pytest.fixture
def wake_clock() -> FakeClock:
    return FakeClock(datetime(2026, 9, 5, 9, 0, tzinfo=UTC))


@pytest.fixture
def queue(pool: ConnectionPool, wake_clock: FakeClock) -> SqliteQueueStore:
    return SqliteQueueStore(pool, clock=wake_clock)


@dataclass
class Harness:
    queue: SqliteQueueStore
    tick: DeliveryTick
    carrier: RecordingCarrier
    injector: RecordingInjector
    directory: FakeDirectory
    findings: RecordingFindings
    clock: FakeClock

    def enqueue(self, key: str, *, receiver: str = SEAT, sender: str = "", **extra: object):
        return self.queue.enqueue(
            EnqueueDraft(
                idempotency_key=key,
                receiver_id=receiver,
                sender_id=sender,
                mode=QueueMode.LIVE,
                **extra,  # type: ignore[arg-type]
            )
        )

    def lease_period(self) -> None:
        """Advance to the next lease period the way the server reaches one.

        Three steps, because the re-offer is not instantaneous: the lease has to
        expire, a tick has to ``reclaim`` it, and the flat backoff it adds to
        ``available_at`` has to elapse before the next ``claim`` can see it.
        Collapsing that into one jump would test a schedule the server never
        runs, and the backoff is the term I3's arithmetic is computed with.
        """
        self.clock.advance(seconds=DELIVERY_LEASE_S + 1)
        self.tick.run_once(now=self.clock.now())
        self.clock.advance(seconds=DELIVERY_BACKOFF_S + 1)


@pytest.fixture
def harness(queue: SqliteQueueStore, wake_clock: FakeClock) -> Harness:
    carrier = RecordingCarrier()
    injector = RecordingInjector()
    directory = FakeDirectory()
    findings = RecordingFindings()
    wake = WakeService(
        store=queue,
        directory=directory,
        carrier=carrier,
        injector=injector,
        clock=wake_clock,
    )
    tick = DeliveryTick(
        store=queue,
        wake=wake,
        directory=directory,
        findings=findings,  # type: ignore[arg-type]
        clock=wake_clock,
        position=SwitchPosition.ON,
    )
    return Harness(queue, tick, carrier, injector, directory, findings, wake_clock)


# ------------------------------------------------------- case 16: the ordinal


def test_case16_one_wake_per_lease_with_an_advancing_ordinal(harness: Harness) -> None:
    """Three lease periods, ``k`` unchanged: ``wake_count`` reaches exactly 3.

    And each lease emits a LINE — the direction §A1.2 exists to prevent is a
    wake path that is silent without saying so, so a run where the second and
    third leases produce no line fails as surely as one where the count runs
    away.
    """
    harness.enqueue("k1")

    harness.tick.run_once(now=harness.clock.now())
    for _ in range(2):
        harness.lease_period()
        harness.tick.run_once(now=harness.clock.now())

    digest = harness.queue.open_digest(SEAT)
    assert digest is not None
    assert digest.wake_count == 3, "the ordinal must advance once per lease period"
    assert len(harness.carrier.writes) == 3, "each lease's line must reach the socket"
    ordinals = [f"wake={n}" for n in (1, 2, 3)]
    assert [w for w in ordinals if any(w in e.line for e in harness.carrier.writes)] == ordinals


def test_case16_wake_count_never_exceeds_the_lease_count(harness: Harness) -> None:
    """I3, in the countable form ``wake_count`` gives it.

    Ticking repeatedly INSIDE one lease must add neither a write nor a count: the
    rows are still leased, so the tick claims nothing and there is no new lease
    period to be re-offered in.
    """
    harness.enqueue("k1")
    harness.tick.run_once(now=harness.clock.now())

    for _ in range(5):
        harness.clock.advance(seconds=1)
        harness.tick.run_once(now=harness.clock.now())

    digest = harness.queue.open_digest(SEAT)
    assert digest is not None and digest.wake_count == 1
    assert len(harness.carrier.calls) == 1


def test_case16_a_second_emission_inside_one_lease_is_dropped_by_the_window(
    harness: Harness,
) -> None:
    """The forced re-emission of case 16's second half.

    Called through ``wake_seat`` DIRECTLY rather than through the tick, because
    the tick would open no new lease and so would neither increment nor re-emit —
    which is why the blueprint names the seam.  The window drops the
    byte-identical payload, so the write count does not move even though the
    carrier was called.
    """
    harness.enqueue("k1")
    harness.tick.run_once(now=harness.clock.now())
    assert len(harness.carrier.writes) == 1

    digest = harness.queue.open_digest(SEAT)
    assert digest is not None
    resolution = harness.directory.resolve(SEAT)
    # The same ordinal and the same ids: byte-identical by design (§A1.2).
    line = harness.carrier.writes[0].line
    harness.tick._wake.wake_seat(  # noqa: SLF001 — the named seam
        digest, resolution, line=line, wake_count=digest.wake_count
    )

    assert len(harness.carrier.calls) == 2
    assert len(harness.carrier.writes) == 1, "the transport must drop the identical payload"


def test_mutant_a_wake_line_without_an_advancing_ordinal_goes_silent(
    harness: Harness,
) -> None:
    """§14's first A1 build note, as an executable mutant.

    Compose the line with a FIXED ordinal — the mutant a builder writes by
    bumping ``wake_count`` after the write, or by leaving ``wake=`` off the line
    — and every re-emission after the first is swallowed by the content window
    while the write reports success.  The test asserts the mutant's symptom, so
    it fails the moment the shipped line stops varying.
    """
    harness.enqueue("k1")
    harness.tick.run_once(now=harness.clock.now())
    baseline = len(harness.carrier.writes)

    digest = harness.queue.open_digest(SEAT)
    assert digest is not None
    resolution = harness.directory.resolve(SEAT)
    frozen = "[cao] digest epoch=1 msgs=1 wake=1 ids=X. Drain: list_messages(epoch=1)"
    for _ in range(3):
        # The clock moves and the LINE does not, which is precisely the mutant:
        # a builder who bumps the ordinal after the write, or leaves ``wake=``
        # off the line, produces exactly this sequence.  The tick is not run
        # here, because the shipped tick would vary the line and hide the defect.
        harness.clock.advance(seconds=DELIVERY_LEASE_S + DELIVERY_BACKOFF_S + 1)
        harness.tick._wake.wake_seat(digest, resolution, line=frozen, wake_count=1)  # noqa: SLF001

    # One write for the frozen line, and nothing after it: three "successful"
    # wakes that never reached the socket.
    assert len(harness.carrier.writes) == baseline + 1
    assert len(harness.carrier.calls) == baseline + 3


# ---------------------------------------- case 17: zero composer writes, and one emitter


def test_case17_a_seat_receiver_is_never_pasted(harness: Harness) -> None:
    """The count of pane writes whose target is the seat is NIL.

    Measured at the injector seam rather than by reading a pane, so a paste that
    is immediately overwritten still counts.  One write in the ``on`` arm fails
    the case whatever its text, since the criterion is the CHANNEL and not the
    wording.
    """
    harness.enqueue("k1")
    harness.enqueue("k2")
    harness.tick.run_once(now=harness.clock.now())

    assert harness.injector.pastes == []
    assert len(harness.carrier.writes) == 1


def test_case17_exactly_one_emitter_fires_per_id_per_lease(harness: Harness) -> None:
    """#613's criterion: one emitter per msg_id per lease period.

    The off-arm samples recorded three surfaces and two wake emitters for one
    id.  Here every claimed row gets exactly one attempt row, and every attempt
    row names the SAME carrier — two carriers over one id in one lease is the
    defect, even when both are native.
    """
    first = harness.enqueue("k1")
    second = harness.enqueue("k2")
    harness.tick.run_once(now=harness.clock.now())

    for msg_id in (first.msg_id, second.msg_id):
        attempts = harness.queue.attempts_for(msg_id)
        assert len(attempts) == 1, "one emitter per id per lease"
        assert attempts[0].carrier == "seat_wake"


def test_case17_a_worker_receiver_still_gets_the_pane(harness: Harness) -> None:
    """K8 kills the seat's REACHABILITY of the seam, not the seam.

    Codex, kiro, cline and grok workers publish no ``messagingSocketPath``, so
    for them the composer is the only channel — killing the paste path outright
    would leave every worker unreachable.
    """
    harness.enqueue("w1", receiver=WORKER)
    harness.tick.run_once(now=harness.clock.now())

    assert len(harness.injector.pastes) == 1
    assert harness.carrier.writes == []


def test_case17_the_ban_does_not_consult_the_switch(queue: SqliteQueueStore, wake_clock) -> None:
    """The paste ban is a property of the RECEIVER'S ROLE, in every position.

    Muting follows the switch position and the ban does not.  ``drain`` is the
    position D9's boot guard can impose without an operator asking for it, so a
    ban scoped to ``on`` would be false in the one position nobody chose.
    """
    for position in (SwitchPosition.OFF, SwitchPosition.SHADOW, SwitchPosition.DRAIN):
        carrier = RecordingCarrier()
        injector = RecordingInjector()
        directory = FakeDirectory()
        wake = WakeService(
            store=queue,
            directory=directory,
            carrier=carrier,
            injector=injector,
            clock=wake_clock,
        )
        tick = DeliveryTick(
            store=queue,
            wake=wake,
            directory=directory,
            findings=None,
            clock=wake_clock,
            position=position,
        )
        queue.enqueue(
            EnqueueDraft(
                idempotency_key=f"pos-{position.value}",
                receiver_id=SEAT,
                mode=QueueMode.LIVE,
            )
        )
        tick.run_once(now=wake_clock.now())
        assert injector.pastes == [], f"a seat paste appeared under {position.value}"
        # And silence is not a pass: the arm must observe an actual emission.
        assert carrier.writes, f"no wake was emitted under {position.value}"
        tick.run_once(now=wake_clock.now())  # let the lease expire cleanly
        wake_clock.advance(seconds=DELIVERY_LEASE_S + DELIVERY_BACKOFF_S + 2)


# ------------------------------------------- case 18: the carrier refuses, seat alive


def test_case18_an_unreachable_carrier_spends_no_attempt_and_raises_once(
    harness: Harness,
) -> None:
    """Kill the socket without killing the seat.

    Each attempt writes ``wake_unreachable`` and does NOT increment ``attempts``;
    the first raises the finding, and exactly ONE finding exists for the epoch
    however many rows it holds — ``k`` messages behind one dead socket are one
    condition, not ``k``.  A run where ``attempts`` moved is the 325-second
    budget killing messages to a healthy seat, which is what §A1.4 exists to
    prevent.
    """
    first = harness.enqueue("k1")
    harness.enqueue("k2")
    harness.carrier.reason = "socket_enoent"

    for _ in range(3):
        harness.tick.run_once(now=harness.clock.now())
        harness.lease_period()

    row = harness.queue.get(first.msg_id)
    assert row is not None
    assert row.attempts == 0, "wake_unreachable is bounded by dead_by, not by attempts"
    assert row.state is not MsgState.DEAD
    outcomes = {a.outcome for a in harness.queue.attempts_for(first.msg_id)}
    assert outcomes == {AttemptOutcome.WAKE_UNREACHABLE}

    findings = harness.findings.of(FindingCode.DIAG_SEAT_WAKE_UNREACHABLE)
    assert len(findings) == 1, "one finding per open epoch, not one per row"
    assert "socket_enoent" in str(findings[0]["detail"])
    assert harness.queue.open_digest(SEAT) is not None, "the epoch stays open"


def test_case18_the_rows_die_on_the_deadline_when_the_carrier_never_heals(
    harness: Harness,
) -> None:
    """The third arm: hold the socket dead past ``dead_by``.

    The rows reach ``delivery_dead`` with ``max_lifetime``, raise
    ``DIAG-DELIVERY-TIME-BOUND``, and the sender notice is enqueued into the
    SENDING WORKER'S mailbox — whose pane is not killed, so a human sees a line
    in the place they are already looking.  That is case 15's shape for the one
    receiver whose own pane can no longer be written to.
    """
    row = harness.enqueue("k1", sender=WORKER)
    harness.carrier.reason = "socket_econnrefused"
    harness.tick.run_once(now=harness.clock.now())

    harness.clock.advance(seconds=DELIVERY_MAX_LIFETIME_S + 1)
    report = harness.tick.run_once(now=harness.clock.now())

    dead = harness.queue.dead_letter(row.msg_id)
    assert dead is not None and dead.reason is DeadReason.MAX_LIFETIME
    assert harness.findings.of(FindingCode.DIAG_DELIVERY_TIME_BOUND)
    assert report.notices_enqueued == 1
    notices = [r for r in harness.queue.undelivered_ids(WORKER)]
    assert notices, "the sender's notice must be a real queue row"


def test_case18_a_stale_record_annotates_and_the_wake_is_still_emitted(
    harness: Harness,
) -> None:
    """The arm #613 sample 5 makes mandatory.

    ``updatedAt`` is written by Claude Code, so its age measures how long the
    seat has been QUIET — unbounded for an idle seat.  A run where a stale record
    suppresses the emission fails the case, because that is #604 arriving through
    the gate meant to prevent it.
    """
    row = harness.enqueue("k1")
    harness.carrier.annotations = ("record_stale",)
    harness.tick.run_once(now=harness.clock.now())

    assert len(harness.carrier.writes) == 1, "a stale record must not suppress the emission"
    attempts = harness.queue.attempts_for(row.msg_id)
    assert attempts[0].outcome is AttemptOutcome.DELIVERED
    assert "record_stale" in attempts[0].detail
    assert harness.findings.of(FindingCode.DIAG_SEAT_WAKE_UNREACHABLE) == []


def test_case18_an_identity_refusal_takes_the_attempt_budget(harness: Harness) -> None:
    """``wake_unresolvable`` for the refusals that cannot heal in a row's life.

    A PID-reuse mismatch clears only when the SESSION restarts, so the deadline
    bound's healing argument does not apply and D12's own principle — a condition
    that cannot clear should die faster — puts it on the attempt budget.
    """
    row = harness.enqueue("k1")
    harness.carrier.reason = "proc_start_mismatch"

    harness.tick.run_once(now=harness.clock.now())
    harness.lease_period()
    harness.tick.run_once(now=harness.clock.now())

    current = harness.queue.get(row.msg_id)
    assert current is not None and current.attempts >= 1
    outcomes = {a.outcome for a in harness.queue.attempts_for(row.msg_id)}
    assert outcomes == {AttemptOutcome.WAKE_UNRESOLVABLE}


# ------------------------------- case 19: the sender, and the two non-refusals


def test_case19_a_single_sender_epoch_is_worker_named(harness: Harness) -> None:
    harness.enqueue("k1", sender="cline-f5d402c6")
    harness.tick.run_once(now=harness.clock.now())

    emitted = harness.carrier.writes[0]
    assert emitted.sender_key == "cline-f5d402c6"
    assert emitted.sender_key != SEAT


def test_case19_a_multi_sender_epoch_names_the_carrier_and_the_count(
    harness: Harness,
) -> None:
    """Naming one of several senders would be false, so the wake names neither."""
    harness.enqueue("k1", sender="worker-a")
    harness.enqueue("k2", sender="worker-b")
    harness.tick.run_once(now=harness.clock.now())

    emitted = harness.carrier.writes[0]
    assert emitted.sender_key == "cao-delivery"
    assert emitted.sender_name == "2 workers"


def test_case19_a_self_addressed_sender_is_substituted_and_still_emitted(
    harness: Harness,
) -> None:
    """A SUBSTITUTION, not a refusal (#381, and the correction to an earlier draft).

    Server-generated notices addressed to the seat legitimately resolve a sender
    that IS the seat.  Refusing would park a real message behind a guard until
    its deadline, so the resolver substitutes the carrier identity and records
    ``sender_substituted`` — no outcome, no bound and no finding, because nothing
    failed.
    """
    row = harness.enqueue("k1", sender=SEAT)
    harness.tick.run_once(now=harness.clock.now())

    emitted = harness.carrier.writes[0]
    assert emitted.sender_key == "cao-delivery"
    attempts = harness.queue.attempts_for(row.msg_id)
    assert attempts[0].outcome is AttemptOutcome.DELIVERED
    assert "sender_substituted" in attempts[0].detail
    assert harness.findings.of(FindingCode.DIAG_SEAT_WAKE_UNREACHABLE) == []


def test_case19_one_unverified_write_raises_nothing(harness: Harness) -> None:
    """A seat too slow for the five-second poll is BUSY, not unreachable.

    ``verify_wake`` polls a timestamp Claude Code writes, so a compacting seat
    fails it with the message sitting in its queue.  A run that raised the
    finding here would be a finding firing in normal operation, which is one
    nobody reads.
    """
    row = harness.enqueue("k1")
    harness.carrier.verified = False
    harness.tick.run_once(now=harness.clock.now())

    attempts = harness.queue.attempts_for(row.msg_id)
    assert attempts[0].outcome is AttemptOutcome.EMITTED_UNVERIFIED
    assert harness.findings.of(FindingCode.DIAG_SEAT_WAKE_UNREACHABLE) == []
    current = harness.queue.get(row.msg_id)
    assert current is not None and current.attempts == 0, "an emitted wake spends no attempt"


def test_case19_three_unconfirmed_leases_raise_the_streak_finding(harness: Harness) -> None:
    """A hung seat becomes diagnosable AS an unreachable seat (#604).

    Three leases is 180 seconds or more, comfortably past a compaction, so the
    finding stays out of normal operation while a session that is
    dead-but-listening — writing no errno and moving no ``statusUpdatedAt`` —
    is reported.  It changes no bound: the row stays on its ordinary lease.
    """
    row = harness.enqueue("k1")
    harness.carrier.verified = False

    for _ in range(UNVERIFIED_STREAK_LEASES):
        harness.tick.run_once(now=harness.clock.now())
        harness.lease_period()

    findings = harness.findings.of(FindingCode.DIAG_SEAT_WAKE_UNREACHABLE)
    assert len(findings) == 1
    assert "unverified_streak" in str(findings[0]["detail"])
    current = harness.queue.get(row.msg_id)
    assert current is not None and current.state is not MsgState.DEAD


# ------------------------------------------------- the dispatch defect (paste_attempted)


def test_a_seat_row_reaching_the_worker_injector_is_refused(harness: Harness) -> None:
    """``inject_worker`` re-asserts the probe and REFUSES rather than pasting.

    A seat row here arrived through a dispatch defect, and the dispatch is
    deterministic — the next lease routes it identically — so it spends the
    attempt budget and dies at 325 s with the finding, loud rather than silent.
    """
    harness.enqueue("k1")
    harness.tick.run_once(now=harness.clock.now())
    digest = harness.queue.open_digest(SEAT)
    assert digest is not None
    resolution = harness.directory.resolve(SEAT)

    report = harness.tick._wake.inject_worker(  # noqa: SLF001
        digest, resolution, line="anything", wake_count=1
    )

    assert report.outcome is AttemptOutcome.PASTE_ATTEMPTED
    assert report.finding_reason == "paste_attempted"
    assert harness.injector.pastes == [], "the seat's composer must never be written to"


def test_a_receiver_with_no_live_incarnation_writes_pane_absent(harness: Harness) -> None:
    """D10: zero live terminals holds the digest open and ages the rows out."""
    row = harness.enqueue("k1", receiver=WORKER)
    harness.directory.terminals[WORKER] = ""
    harness.tick.run_once(now=harness.clock.now())

    attempts = harness.queue.attempts_for(row.msg_id)
    assert attempts[0].outcome is AttemptOutcome.PANE_ABSENT
    assert harness.carrier.writes == [] and harness.injector.pastes == []
    digest = harness.queue.open_digest(WORKER)
    assert digest is not None and digest.open
    assert digest.wake_count == 0, "a wake nobody could receive must not spend an ordinal"


def test_the_id_list_is_capped_and_the_line_stays_one_line(harness: Harness) -> None:
    """A formatting bound, not a timing constant: ``k`` has no ceiling."""
    for n in range(13):
        harness.enqueue(f"k{n}")
    harness.tick.run_once(now=harness.clock.now())

    line = harness.carrier.writes[0].line
    assert "msgs=13" in line
    assert "+3 more" in line
    assert "\n" not in line


def test_an_epoch_whose_messages_all_end_closes_rather_than_staying_open(
    harness: Harness,
) -> None:
    """I6: an open digest that cannot close is what the phase forbids."""
    row = harness.enqueue("k1")
    harness.tick.run_once(now=harness.clock.now())
    digest = harness.queue.open_digest(SEAT)
    assert digest is not None

    harness.queue.settle(row.msg_id, state=MsgState.DELIVERED, now=harness.clock.now())
    harness.clock.advance(seconds=1)
    harness.tick.run_once(now=harness.clock.now())

    closed = harness.queue.digest_at(SEAT, digest.epoch)
    assert closed is not None and not closed.open
    assert closed.consumed_via == "cancelled", "a live receiver's epoch is cancelled, not abandoned"


def test_a_reaped_receivers_epoch_closes_abandoned(harness: Harness) -> None:
    """D10's second conjunct, evidenced from the probe rather than assumed."""
    row = harness.enqueue("k1", receiver=WORKER)
    harness.tick.run_once(now=harness.clock.now())
    digest = harness.queue.open_digest(WORKER)
    assert digest is not None

    harness.directory.panes[WORKER] = False
    harness.queue.settle(row.msg_id, state=MsgState.SUPERSEDED, now=harness.clock.now())
    harness.clock.advance(seconds=1)
    harness.tick.run_once(now=harness.clock.now())

    closed = harness.queue.digest_at(WORKER, digest.epoch)
    assert closed is not None and closed.consumed_via == "abandoned"


def test_rows_arriving_during_an_open_epoch_are_still_woken_about(harness: Harness) -> None:
    """The additive extension, and why it is not §5 item 6's rewrite.

    Strict immutability would leave every row that arrives while an epoch is open
    outside EVERY digest until that epoch closes — and behind a seat that is not
    acking, those rows would reach ``dead_by`` never having been woken about,
    which is #604's shape for exactly the messages this phase exists to deliver.
    Nothing here removes an id or moves one; the re-parent remains the single
    event that rewrites a digest.
    """
    harness.enqueue("k1")
    harness.tick.run_once(now=harness.clock.now())
    first = harness.queue.open_digest(SEAT)
    assert first is not None and len(first.msg_ids) == 1

    harness.enqueue("k2")
    harness.lease_period()
    harness.tick.run_once(now=harness.clock.now())

    second = harness.queue.open_digest(SEAT)
    assert second is not None
    assert second.epoch == first.epoch, "no second epoch while the first is open"
    assert len(second.msg_ids) == 2
    assert "msgs=2" in harness.carrier.writes[-1].line


def test_retention_keeps_open_digests_and_finding_evidence(
    harness: Harness,
) -> None:
    """Case 14 over the new tables: what ages out, and the two things that never do."""
    old = harness.enqueue("old")
    harness.queue.settle(old.msg_id, state=MsgState.DELIVERED, now=harness.clock.now())
    kept = harness.enqueue("kept")
    harness.tick.run_once(now=harness.clock.now())

    harness.clock.advance(days=31)
    removed = harness.tick.prune(now=harness.clock.now())

    assert removed >= 1
    assert harness.queue.get(old.msg_id) is None
    assert harness.queue.get(kept.msg_id) is not None, "a non-terminal row is never pruned"
    assert harness.queue.open_digest(SEAT) is not None, "an open digest is never pruned"


def test_a_notice_that_dead_letters_enqueues_no_second_notice(harness: Harness) -> None:
    """D14: the chain is one notice deep BY CONSTRUCTION, not by rate."""
    harness.enqueue("n1", sender=WORKER, is_notice=True)
    harness.clock.advance(seconds=DELIVERY_MAX_LIFETIME_S + 1)
    report = harness.tick.run_once(now=harness.clock.now())

    assert report.dead_count if hasattr(report, "dead_count") else len(report.dead)
    assert report.notices_enqueued == 0


def test_a_dialog_held_row_is_re_offered_without_spending_an_attempt(
    harness: Harness,
) -> None:
    """D12's separate bounds, and the accounting that makes them separate.

    A blanket increment in ``reclaim`` would put a worker waiting on a human onto
    the same 325-second budget as a poison message, which is the r4 draft the
    design rejected.
    """
    row = harness.enqueue("w1", receiver=WORKER)
    harness.injector.outcome = AttemptOutcome.VETO_DIALOG
    harness.tick.run_once(now=harness.clock.now())
    harness.lease_period()
    harness.tick.run_once(now=harness.clock.now())

    current = harness.queue.get(row.msg_id)
    assert current is not None
    assert current.attempts == 0, "veto_dialog is bounded by its ceiling, not by attempts"
    assert current.held_since is not None


def test_a_dialog_hold_past_the_ceiling_dies_with_its_own_reason(harness: Harness) -> None:
    row = harness.enqueue("w1", receiver=WORKER)
    harness.injector.outcome = AttemptOutcome.VETO_DIALOG
    harness.tick.run_once(now=harness.clock.now())

    harness.clock.advance(seconds=1600)
    harness.tick.run_once(now=harness.clock.now())

    dead = harness.queue.dead_letter(row.msg_id)
    assert dead is not None and dead.reason is DeadReason.VETO_CEILING


def test_the_completion_cancel_reaches_a_steer_supersede_key_cannot(
    harness: Harness,
) -> None:
    """D8, and the limit stated rather than hidden: ``ready`` rows only."""
    steer = harness.enqueue("s1", receiver=WORKER, cancel_on_complete=True)
    plain = harness.enqueue("s2", receiver=WORKER)

    cancelled = harness.queue.cancel_on_complete(WORKER, now=harness.clock.now())

    assert cancelled == (steer.msg_id,)
    assert harness.queue.get(steer.msg_id).state is MsgState.SUPERSEDED  # type: ignore[union-attr]
    assert harness.queue.get(plain.msg_id).state is MsgState.READY  # type: ignore[union-attr]


def test_the_flip_sweeps_an_unresolved_shadow_row(harness: Harness) -> None:
    """§7a's stranded shadow row, ended rather than left open forever."""
    stray = harness.queue.enqueue(
        EnqueueDraft(idempotency_key="stray", receiver_id=SEAT, mode=QueueMode.SHADOW)
    )
    swept = harness.queue.sweep_shadow(now=harness.clock.now())

    assert swept == 1
    row = harness.queue.get(stray.msg_id)
    assert row is not None and row.state is MsgState.SUPERSEDED


def test_a_shadow_row_is_never_claimed_or_woken_about(harness: Harness) -> None:
    """The ``mode='live'`` filter, from the tick's side of it."""
    harness.queue.enqueue(
        EnqueueDraft(idempotency_key="obs", receiver_id=SEAT, mode=QueueMode.SHADOW)
    )
    harness.tick.run_once(now=harness.clock.now())

    assert harness.carrier.writes == []
    assert harness.queue.open_digest(SEAT) is None


def test_a_delivered_wake_leaves_the_row_leased_until_it_is_acked(harness: Harness) -> None:
    """§5b: a seat that reads the line and stops without acking is SUPPORTED.

    The epoch stays open, the lease expires, ``reclaim`` re-offers, the same
    epoch is re-woken with a fresh ordinal — I1 and I3 holding together — and no
    attempt is spent, because nothing failed.
    """
    row = harness.enqueue("k1")
    harness.tick.run_once(now=harness.clock.now())

    leased = harness.queue.get(row.msg_id)
    assert leased is not None and leased.state is MsgState.LEASED

    harness.lease_period()
    harness.tick.run_once(now=harness.clock.now())
    after = harness.queue.get(row.msg_id)
    assert after is not None and after.attempts == 0
    digest = harness.queue.open_digest(SEAT)
    assert digest is not None and digest.wake_count == 2


def test_the_lease_that_expires_with_nothing_recorded_spends_an_attempt(
    queue: SqliteQueueStore, wake_clock: FakeClock
) -> None:
    """Nothing was observed, so the honest accounting is the failing one.

    The alternative lets a row whose injector never runs live to its deadline
    re-offering forever, which is a pending row with no attempt bound.
    """
    row = queue.enqueue(EnqueueDraft(idempotency_key="k1", receiver_id=SEAT, mode=QueueMode.LIVE))
    queue.claim(lease_owner="tick", now=wake_clock.now())
    wake_clock.advance(seconds=DELIVERY_LEASE_S + 1)

    result = queue.reclaim(now=wake_clock.now())

    assert (result.reoffered, result.incremented) == (1, 1)
    current = queue.get(row.msg_id)
    assert current is not None and current.attempts == 1
    assert current.dead_by == row.dead_by


def test_the_deadline_is_never_recomputed_across_a_full_budget(
    queue: SqliteQueueStore, wake_clock: FakeClock
) -> None:
    """D12's named mutant, re-asserted under 3b's per-outcome accounting.

    The accounting change touches the same UPDATE, so this is the place a
    ``dead_by`` recomputation would be reintroduced.
    """
    row = queue.enqueue(EnqueueDraft(idempotency_key="k1", receiver_id=SEAT, mode=QueueMode.LIVE))
    stamped = row.dead_by
    for _ in range(4):
        wake_clock.advance(seconds=DELIVERY_BACKOFF_S + 1)
        queue.claim(lease_owner="tick", now=wake_clock.now())
        wake_clock.advance(seconds=DELIVERY_LEASE_S + 1)
        queue.reclaim(now=wake_clock.now())
        current = queue.get(row.msg_id)
        assert current is not None and current.dead_by == stamped


def test_the_digest_line_carries_ids_and_never_a_body(harness: Harness) -> None:
    """I3's line, and the half of #488/#314 this phase declines.

    Carrying the ids costs a bounded number of tokens and lets a reader match the
    wake against ``cao diag <msg_id>`` without a round trip; carrying the bodies
    costs ``k`` message bodies in the seat's context per wake, which is what
    #613's first observation caught happening.
    """
    body = "the worker's actual callback text that must never ride the wake"
    row = harness.enqueue("k1", payload=body)
    harness.tick.run_once(now=harness.clock.now())

    line = harness.carrier.writes[0].line
    assert body not in line
    assert row.msg_id in line
    assert "list_messages(epoch=1)" in line


def test_an_epoch_that_closes_mid_tick_records_no_attempt(harness: Harness) -> None:
    """The one report that produces no attempt rows, and why that matters.

    A digest can close between the tick reading it and the emission — the seat
    acked, or a re-parent emptied it — and no carrier then runs. Writing
    ``delivered`` anyway would tell ``cao diag <msg_id>`` that a wake landed when
    none was composed, which is the pane archaeology I5 exists to end; and since
    ``delivered`` spends no attempt, the rows would re-offer to their deadline
    with a delivery on the record and nothing delivered.
    """
    row = harness.enqueue("k1")
    harness.tick.run_once(now=harness.clock.now())
    digest = harness.queue.open_digest(SEAT)
    assert digest is not None

    harness.queue.close_digest(SEAT, digest.epoch, via="mcp_ack", now=harness.clock.now())
    resolution = harness.directory.resolve(SEAT)
    before = len(harness.queue.attempts_for(row.msg_id))

    report = harness.tick._wake.deliver(digest, ())  # noqa: SLF001

    assert report.recordable is False
    assert report.emitted is False
    assert report.detail == "epoch_closed"
    assert len(harness.queue.attempts_for(row.msg_id)) == before
    assert resolution.live
