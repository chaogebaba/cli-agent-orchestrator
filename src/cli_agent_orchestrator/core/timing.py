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
    "GATE_QUESTION_EXPIRY_S",
    "GATE_QUESTION_SWEEP_S",
    "GATE_QUESTION_WAIT_CAP_S",
    "IDLE_STALL_AGE_S",
    "WAKE_MAX_RECORD_AGE_S",
    "NO_SIGNAL_S",
    "PANE_HEARTBEAT_S",
    "PANE_LIVENESS_STALENESS_S",
    "PANE_MISS_TICKS",
    "PANE_SAMPLE_S",
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

#: The pane-delta sampler's re-drive period in seconds (WP-ARCH phase 2, §12).
#:
#: SEPARATE from ``PANE_HEARTBEAT_S`` and strictly smaller, which is the whole
#: point of naming it.  The liveness probe owns two jobs on one task: it lists
#: the fleet's panes (a heartbeat, ``PANE_HEARTBEAT_S``) and it drives
#: ``pane_liveness.observe`` (a sample, this).  Running both at the heartbeat
#: would have been simpler and wrong: the sampler calls its own sample stale
#: after ``PANE_LIVENESS_STALENESS_S``, so once phase 3 deletes the
#: stalled-callback watchdog and this becomes the only driver, a 20-second
#: cadence would leave ``fuse_status``'s rules 3a/3b with no evidence for half of
#: every window — and ``unchanged_count`` would need a minute to reach the
#: stable-sample threshold instead of the 3-15 seconds it takes today.
#:
#: The value MIRRORS the watchdog's own tick ceiling (``min(5.0, ...)``) rather
#: than improving on it: this re-drive replaces that tick, and a faster cadence
#: would change pane-delta timing rather than preserve it.  It does not ADD
#: captures while both drivers are alive, because the drive defers to any sample
#: taken inside the staleness window (the ``peek`` guard in the composition
#: root); a test counts the captures to keep that true.
PANE_SAMPLE_S = 5

#: How long ``services/pane_liveness.py`` treats a sample as fresh, MIRRORED here
#: for the reason ``IDLE_STALL_AGE_S`` is mirrored: ``core`` may not import a
#: legacy service, and the ordering below has to be raisable at import.  The real
#: definition is ``pane_liveness._STALENESS_S`` and it stays there; a test asserts
#: the two agree, and that test is what catches a retune that moved one and not
#: the other — a drift that would silently blind the pane-delta rules for part of
#: every window with both files looking correct on their own.
PANE_LIVENESS_STALENESS_S = 10.0

#: F935 (#787): how long launch health waits for the terminal backend to NAME an
#: agent in a freshly launched pane before declaring the seat unusable.
#:
#: Proving a PROCESS is alive is not the same as proving an AGENT is there. A
#: wrapper, launcher or runtime that starts and stays up satisfies
#: ``probe_provider_liveness`` (its foreground process is not the baseline
#: shell) even when the agent inside it never came up — and a seat the backend
#: never recognises has no native status, no lifecycle edges, and nothing
#: delivery can wait on. It looks healthy and stalls forever.
#:
#: Measured on herdr 0.9.0 (protocol 22), polling panes once a second from the
#: moment the launch command was sent:
#:
#:     live pane   no agent (0s) -> agent named (1s) -> classified idle (4s)
#:     crashed     agent NEVER named, indefinitely
#:     bare shell  agent NEVER named, indefinitely
#:
#: The name appears about a second in and the pane classifies by four; absence
#: is permanent when nothing is alive. 30 s is therefore ~7x the observed
#: classification time — deliberately generous, because the cost of waiting too
#: long is a slower failure while the cost of waiting too little is killing a
#: seat that was merely slow to start (a cold model catalogue, an MCP handshake).
HERDR_AGENT_DETECT_S = 30.0

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
    if PANE_SAMPLE_S * 2 > PANE_LIVENESS_STALENESS_S:
        # The sampler's own freshness rule is "a sample from the last two passes".
        # A drive slower than half the staleness window therefore hands the
        # pane-delta rules a stale sample for part of every window, which reads
        # to them as NO evidence and silently disables the downgrade.
        raise ValueError(
            f"PANE_SAMPLE_S * 2 ({PANE_SAMPLE_S * 2}) must be <= "
            f"PANE_LIVENESS_STALENESS_S ({PANE_LIVENESS_STALENESS_S})"
        )
    if PANE_HEARTBEAT_S % PANE_SAMPLE_S != 0:
        # One task interleaves both cadences, so the heartbeat has to be a whole
        # number of sample ticks; otherwise the listing drifts against the sample.
        raise ValueError(
            f"PANE_HEARTBEAT_S ({PANE_HEARTBEAT_S}) must be a multiple of "
            f"PANE_SAMPLE_S ({PANE_SAMPLE_S})"
        )
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

#: How long a durable gate question stays open before the sweep expires it
#: (WP-ARCH Amendment A slice B1, A2/AC-A7).  An hour is long enough that a
#: supervisor reading a digest between tasks still answers in time, and short
#: enough that a forgotten question surfaces as an anomaly inside one working
#: session rather than sitting open across a restart.  It lives HERE, not next
#: to the service that uses it, because §4c admits exactly one home for a
#: duration and a second declaration is how two numbers come to disagree.
GATE_QUESTION_EXPIRY_S = 3600

#: How often the expiry daemon sweeps overdue questions (slice B2, AC-A7).
#: Thirty seconds is two orders of magnitude finer than the hour a question
#: lives, so an expiry is observed promptly, and coarse enough that the sweep is
#: not competing with the status monitor for the write lock: the sweep reads
#: first and takes a write transaction ONLY when it has something to settle, so
#: an idle fleet costs one SELECT per period and no lock at all.
GATE_QUESTION_SWEEP_S = 30

#: The ceiling on ONE bounded long poll, in seconds.  Deliberately under
#: ``MCP_REQUEST_TIMEOUT`` so the server answers before the client gives up: a
#: caller that times out client-side cannot tell "still waiting" from "the
#: server died", and a blocking asker would then retry an ask it already made.
#: A longer wait is many of these in a row, each a fresh request holding nothing
#: open between them.
GATE_QUESTION_WAIT_CAP_S = 25

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

#: The age past which the seat's Claude Code registry record is refused, MIRRORED
#: from ``supervisor.wake.max_record_age_s`` (``services/config_service.py``) for
#: the same reason ``IDLE_STALL_AGE_S`` is mirrored: ``core`` may not import a
#: legacy service, and T1 has to be raisable at import.  A test asserts the two
#: agree.
WAKE_MAX_RECORD_AGE_S = 900


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
    if WAKE_MAX_RECORD_AGE_S >= DELIVERY_MAX_LIFETIME_S:
        # T1 (§A1.4).  Deliberately in a SEPARATE label space from §3's
        # invariants, which have collided with these since r2: a row must outlive
        # a full staleness window, or a seat whose registry record is merely
        # stale loses its messages before the record can heal.  An operator who
        # raises the config key past the lifetime loses the property, and that
        # duty is written down here rather than mechanised — it is the first
        # constant to revisit if seats are seen dying unreached.
        raise ValueError(
            f"T1: WAKE_MAX_RECORD_AGE_S ({WAKE_MAX_RECORD_AGE_S}) must be < "
            f"DELIVERY_MAX_LIFETIME_S ({DELIVERY_MAX_LIFETIME_S})"
        )


check_delivery_orderings()
