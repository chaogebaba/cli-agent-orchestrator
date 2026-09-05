"""Every phase-1 and phase-3 duration, in one place (WP-ARCH blueprint §4c, §5c).

The rule the blueprint states and this module enforces: **no other module may
hold a literal duration**, and tests import the constants rather than the
numbers.  A test that hard-codes ``60`` passes for the wrong reason the day
``NO_SIGNAL_S`` moves.

The orderings below are not documentation.  They are the difference between a
liveness scheme that degrades a healthy terminal and one that does not, and each
is checked at import so a careless retune fails loudly at server start rather
than quietly at 3am:

* ``ROLLOUT_POLL_MS * 4 <= PANE_HEARTBEAT_S * 1000`` — the rollout tailer must
  stat its file several times per heartbeat, or ``last_source_probe_at`` is
  stale by construction and source health flaps.
* ``NO_SIGNAL_S > PANE_HEARTBEAT_S * 2`` — one missed probe must never degrade a
  terminal.  This is the invariant that keeps a momentary tmux hiccup from
  becoming a fleet of ``degraded(no_signal)`` rows.
* ``PROBE_FAIL_TICKS * PANE_HEARTBEAT_S >= NO_SIGNAL_S`` — a fleet-wide
  ``producer_error`` may not be declared faster than a single terminal's silence
  horizon, or the fleet-wide reason would mask the per-terminal one.
* ``PANE_MISS_TICKS >= 2`` — one miss never exits a process.  ``process.exited``
  is unrecoverable in the projection, so it takes two consecutive successful
  probes that fail to list the pane.

Checked with explicit raises rather than bare ``assert``, because ``python -O``
strips ``assert`` and these orderings must hold in an optimised interpreter too.
"""

from __future__ import annotations

__all__ = [
    "DELIVERY_BACKOFF_S",
    "DELIVERY_DEDUP_WINDOW_S",
    "DELIVERY_INJECT_BUDGET_S",
    "DELIVERY_LEASE_S",
    "DELIVERY_MAX_ATTEMPTS",
    "DELIVERY_MAX_LIFETIME_S",
    "DELIVERY_RETENTION_DAYS",
    "DELIVERY_TICK_S",
    "DELIVERY_VETO_CEILING_S",
    "IDLE_STALL_AGE_S",
    "NO_SIGNAL_S",
    "PANE_HEARTBEAT_S",
    "PANE_MISS_TICKS",
    "PROBE_FAIL_TICKS",
    "RETENTION_DAYS",
    "RETENTION_SWEEP_S",
    "ROLLOUT_POLL_MS",
    "check_delivery_orderings",
    "check_orderings",
]

#: Liveness-probe period in seconds.  One ``tmux list-panes -a -F ...`` per tick
#: for the WHOLE fleet — not one call per terminal — updating
#: ``worker_state_shadow.last_probe_at``/``pane_present``/``pane_pid``/
#: ``miss_count`` as COLUMNS.  Doubles as the projector's sweep period, since a
#: projector never notices silence by itself (r8 N5).
PANE_HEARTBEAT_S = 20

#: Source-health horizon in seconds.  An authoritative source is healthy while
#: its tailer stat-ed the file within this window; ``degraded(no_signal)`` needs
#: BOTH ``last_probe_at`` and ``last_source_probe_at`` older than it.
NO_SIGNAL_S = 60

#: Rollout JSONL tail poll interval in milliseconds.  Every poll that stats the
#: file bumps ``last_source_probe_at``.
ROLLOUT_POLL_MS = 500

#: Consecutive FAILED probes before the fleet is marked
#: ``degraded(producer_error)``.  A failed probe is not pane absence (B13).
PROBE_FAIL_TICKS = 3

#: Consecutive SUCCESSFUL probes that do not list a pane before
#: ``process.exited`` is appended for it.
PANE_MISS_TICKS = 2

#: Event pruning horizon in days.  Rows named by an open finding's
#: ``sample_event_id`` are kept regardless.
RETENTION_DAYS = 30

#: Retention sweep period in seconds.  The blueprint says events are pruned
#: "daily"; the number lives here because §4c forbids a literal duration
#: anywhere else, including in the retention task itself.
RETENTION_SWEEP_S = 24 * 60 * 60


def check_orderings() -> None:
    """Raise ``ValueError`` if any §4c ordering invariant is violated."""
    if ROLLOUT_POLL_MS * 4 > PANE_HEARTBEAT_S * 1000:
        raise ValueError(
            f"ROLLOUT_POLL_MS * 4 ({ROLLOUT_POLL_MS * 4}) must be <= "
            f"PANE_HEARTBEAT_S * 1000 ({PANE_HEARTBEAT_S * 1000})"
        )
    if NO_SIGNAL_S <= PANE_HEARTBEAT_S * 2:
        raise ValueError(
            f"NO_SIGNAL_S ({NO_SIGNAL_S}) must be > PANE_HEARTBEAT_S * 2 "
            f"({PANE_HEARTBEAT_S * 2})"
        )
    if PROBE_FAIL_TICKS * PANE_HEARTBEAT_S < NO_SIGNAL_S:
        raise ValueError(
            f"PROBE_FAIL_TICKS * PANE_HEARTBEAT_S ({PROBE_FAIL_TICKS * PANE_HEARTBEAT_S}) "
            f"must be >= NO_SIGNAL_S ({NO_SIGNAL_S})"
        )
    if PANE_MISS_TICKS < 2:
        raise ValueError(f"PANE_MISS_TICKS ({PANE_MISS_TICKS}) must be >= 2")
    if RETENTION_DAYS < 1:
        raise ValueError(f"RETENTION_DAYS ({RETENTION_DAYS}) must be >= 1")
    if RETENTION_SWEEP_S < PANE_HEARTBEAT_S:
        raise ValueError(
            f"RETENTION_SWEEP_S ({RETENTION_SWEEP_S}) must be >= "
            f"PANE_HEARTBEAT_S ({PANE_HEARTBEAT_S})"
        )


check_orderings()


# ---------------------------------------------------------------------------
# Phase 3 — the delivery queue (blueprint §5c).
#
# These live here for the same reason the phase-1 constants do: §4c forbids a
# duration literal anywhere else, and the orderings between them are not
# documentation but the difference between a queue that redelivers and one that
# steals leases from itself.  Every figure in the invariants below follows from
# this table and nothing else.
# ---------------------------------------------------------------------------

#: The reclaim-and-digest period of the polling safety net (§5c).  Server-side,
#: and that is the load-bearing property: the carrier that failed in #604 was a
#: client-side watcher armed by seat events, and an idle seat emits no event to
#: arm one.
DELIVERY_TICK_S = 10

#: Lease duration issued by ``claim``.  A lease expiring is the ONLY thing that
#: increments ``attempts`` (D3), so this is the unit the attempt budget is
#: measured in.
DELIVERY_LEASE_S = 60

#: Attempts before a row moves to ``delivery_dead`` (audit §3.2).  Spent by
#: ``pane_absent`` and ``veto_unverified``; a ``veto_dialog`` hold does NOT spend
#: one, which is what keeps D12's two budgets separate.
DELIVERY_MAX_ATTEMPTS = 5

#: The injection round-trip a lease must accommodate.  A constant so its
#: ordering against the lease can be raised at import; the MEASURED round-trip
#: is checked against it in AC-3b, a measurement not being available at import.
DELIVERY_INJECT_BUDGET_S = 20

#: Flat delay ``reclaim`` adds to ``available_at`` on each re-offer.  Flat, not
#: exponential, so time-to-dead is a product rather than a summation and
#: invariant I3 stays raisable at import over named constants.  Exponential
#: growth would buy nothing at these magnitudes: five attempts against a
#: 60-second lease already spans five minutes.
DELIVERY_BACKOFF_S = 5

#: How long a row may sit dialog-held before ``delivery_dead`` (D12).  A
#: DURATION rather than an attempt count, because a worker waiting behind an
#: unknown-dialog episode is waiting on a human and routinely outlives five
#: minutes, while a poison message should die fast.  Set far above the attempt
#: budget's span precisely so the two budgets are genuinely separate.
DELIVERY_VETO_CEILING_S = 1500

#: The F475 rolling window the enqueue dedup reproduces (D13).  Mirrors the
#: legacy ``_F475_CALLBACK_DEDUP_WINDOW_S`` at ``clients/database.py:8180``; a
#: test asserts the two agree, since a silent divergence here would change how
#: many messages are delivered.
DELIVERY_DEDUP_WINDOW_S = 60

#: The row's whole life from enqueue, stamped into ``dead_by`` ONCE at enqueue
#: and never rewritten (D12).  Both the attempt budget and the dialog ceiling
#: are conditions that can only bring death forward, so this is the worst case
#: over every outcome sequence — not the sum of the inner spans.
DELIVERY_MAX_LIFETIME_S = 1700

#: How long terminal queue rows and CLOSED digests are kept before the tick
#: prunes them (§13d).  A row named by an OPEN finding is never pruned, exactly
#: as phase 1's ``prune`` keeps open evidence.
DELIVERY_RETENTION_DAYS = 30

#: The legacy stalled-notice age, MIRRORED here rather than imported.
#:
#: The real definition is ``IDLE_STALL_AGE`` at ``services/inbox_service.py:146``
#: and it stays there: ``core`` may not import legacy (the
#: ``new-code-never-imports-legacy`` contract), and §4c forbids the number
#: appearing in a second module as a bare literal.  A mirrored constant with a
#: test asserting equality is the only form that satisfies both, and the test is
#: what catches the drift — invariants I3 and I4 both bound quantities against
#: this value, so a legacy retune that moved it without moving this would leave
#: two invariants passing against a number the server no longer uses.
IDLE_STALL_AGE_S = 1800


def check_delivery_orderings() -> None:
    """Raise ``ValueError`` if any §5c ordering invariant is violated.

    Read in order, each is a real failure mode rather than a tidiness rule:

    * **I1** a tick that cannot run before the lease expires turns delivery into
      redelivery, every time.
    * **I2** a lease shorter than the liveness period expires before the probe
      that would show the pane alive, so ``reclaim`` steals from itself.
    * **I3** the attempt budget's span is over the LEASE, because ``reclaim``
      increments on lease expiry alone, plus the backoff, because each re-offer
      waits before the next claim.  Multiplying the TICK instead admitted a
      600-second lease reaching 3000 s while still passing; omitting the backoff
      put a delay outside the invariant meant to bound it.
    * **I4** the whole chain.  The dialog ceiling sits far above the attempt
      span, the row's lifetime above that, and the legacy stall age above all of
      it — so a row dies before the legacy notice could speak about it, which is
      the #568 non-overlap property.  The two inner terms are condition spans
      running from first lease; the two outer ones both run from message
      creation, which is what makes the decisive comparison like for like under
      R1's no-delayed-enqueue rule.
    * **I5** an injection that cannot finish inside its own lease has already
      lost the row.

    I3 is conservative by one backoff: a row dies on the fifth increment and the
    true span is 320 s rather than 325 s.  The stated form bounds ABOVE the true
    value and is simpler to raise at import, so it is a ceiling rather than a
    measurement.
    """
    attempt_span = (DELIVERY_LEASE_S + DELIVERY_BACKOFF_S) * DELIVERY_MAX_ATTEMPTS

    if DELIVERY_TICK_S >= DELIVERY_LEASE_S:
        raise ValueError(
            f"I1: DELIVERY_TICK_S ({DELIVERY_TICK_S}) must be < "
            f"DELIVERY_LEASE_S ({DELIVERY_LEASE_S})"
        )
    if PANE_HEARTBEAT_S >= DELIVERY_LEASE_S:
        raise ValueError(
            f"I2: PANE_HEARTBEAT_S ({PANE_HEARTBEAT_S}) must be < "
            f"DELIVERY_LEASE_S ({DELIVERY_LEASE_S})"
        )
    if attempt_span >= IDLE_STALL_AGE_S:
        raise ValueError(
            f"I3: (DELIVERY_LEASE_S + DELIVERY_BACKOFF_S) * DELIVERY_MAX_ATTEMPTS "
            f"({attempt_span}) must be < IDLE_STALL_AGE_S ({IDLE_STALL_AGE_S})"
        )
    if not (attempt_span < DELIVERY_VETO_CEILING_S < DELIVERY_MAX_LIFETIME_S < IDLE_STALL_AGE_S):
        raise ValueError(
            "I4: the chain (lease + backoff) * max_attempts < DELIVERY_VETO_CEILING_S "
            "< DELIVERY_MAX_LIFETIME_S < IDLE_STALL_AGE_S must hold, but reads "
            f"{attempt_span} < {DELIVERY_VETO_CEILING_S} < {DELIVERY_MAX_LIFETIME_S} "
            f"< {IDLE_STALL_AGE_S}"
        )
    if DELIVERY_INJECT_BUDGET_S >= DELIVERY_LEASE_S:
        raise ValueError(
            f"I5: DELIVERY_INJECT_BUDGET_S ({DELIVERY_INJECT_BUDGET_S}) must be < "
            f"DELIVERY_LEASE_S ({DELIVERY_LEASE_S})"
        )
    if DELIVERY_MAX_ATTEMPTS < 1:
        raise ValueError(f"DELIVERY_MAX_ATTEMPTS ({DELIVERY_MAX_ATTEMPTS}) must be >= 1")
    if DELIVERY_RETENTION_DAYS < 1:
        raise ValueError(f"DELIVERY_RETENTION_DAYS ({DELIVERY_RETENTION_DAYS}) must be >= 1")
    if DELIVERY_DEDUP_WINDOW_S < 1:
        raise ValueError(f"DELIVERY_DEDUP_WINDOW_S ({DELIVERY_DEDUP_WINDOW_S}) must be >= 1")


check_delivery_orderings()
