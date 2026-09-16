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
    "ACP_CANCEL_SETTLE_S",
    "ACP_KILL_GRACE_S",
    "ACP_WRITE_SETTLE_S",
    "BUSY_CREDIT_CAP_S",
    "BUSY_CREDIT_MARGIN_S",
    "CANCEL_HOLD_MARGIN_S",
    "CUT_HISTORY_N",
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
    "HERDR_PROMPT_WAIT_MS",
    "HERDR_SUBMISSION_GATE_MS",
    "IDLE_STALL_AGE_S",
    "INTERRUPT_BUDGET_N",
    "INTERRUPT_BUDGET_WINDOW_S",
    "INTERRUPT_MAX_LATENCY_S",
    "INTERRUPT_MIN_GAP_S",
    "RECOVERY_DEADLINE_S",
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

#: herdr's OWN submission gate, MIRRORED here rather than measured (WP-HERDR H2).
#:
#: A herdr fact, not ours: ``herdr agent prompt --help`` on 0.9.0 states that an
#: accepted submission starting from a non-working state "requires an observed
#: working or blocked state within 5000ms; otherwise it returns
#: agent_prompt_stalled".  It is mirrored for the same reason
#: ``IDLE_STALL_AGE_S`` is — the number has to be raisable at import to bound the
#: constant below, and ``core`` may not read a help text — and it is re-certified
#: with the protocol pin, never drifted.
HERDR_SUBMISSION_GATE_MS = 5000

#: The caller-side bound on Seam B's ``agent.prompt`` wait (WP-HERDR H2).
#:
#: Sits BETWEEN two numbers, and both bounds are real failure modes rather than
#: taste — see B1 and B2 in :func:`check_delivery_orderings`.  Below herdr's own
#: gate it would convert every stall into the coarser ``timeout`` code and throw
#: away the distinction the gate exists to draw; above the injection budget one
#: submission could outlive the round-trip the lease was sized for.
HERDR_PROMPT_WAIT_MS = 8000

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


# ---------------------------------------------------------------------------
# WP-ACP-PLANE S1 — the interrupt plane (D6b, D7.3) and the ACP transport.
#
# The nine literals below were FROZEN by the supervisor on 2026-09-16 under
# AC-S1.24 (signature delegated by the user via AskUserQuestion), against the
# S0 round-2 measurements in ``supervisor-protocol/s0-round2.md``
# §"AC-S1.24 freeze proposal".  AC-S1.24's fails-if is "a constant is changed
# after the AC that uses it was signed", so each carries its measurement or
# derivation BESIDE it rather than in a design document free to drift from the
# number.  AC-S1.22, AC-S0.10, AC-S1.27 and AC-S1.29 are signed only against
# these values.
#
# They live here for §4c's reason and no other: no module may hold a literal
# duration, and the orderings between them have to be raisable at import.
# ---------------------------------------------------------------------------

#: The SAFETY bound after which an unsettled ``session/cancel`` is quarantined
#: (D6b(2)); also D6 rule 4's steer-cancel floor.
#:
#: FROZEN 2026-09-16 at **20**.  S0 round 2 took 90 cancel samples across six
#: adapters (6 x 3 arms x 5); every cancel that settled at all landed within
#: 0.273 s, p95 0.076 s, so this bound is never binding in the measured fleet —
#: it exists for the adapter that hangs, not for the ones that answer.  It is
#: deliberately far above the measurement and still below ``DELIVERY_LEASE_S``
#: (60), because I's cancelling reservation is ``ACP_CANCEL_SETTLE_S +
#: CANCEL_HOLD_MARGIN_S`` and that sum has to fit inside one lease.
ACP_CANCEL_SETTLE_S = 20

#: The PRODUCT definition of "seen immediately" (D6b(2)): elapsed time from
#: admission of an interrupt to the urgent prompt being accepted as the next
#: model-visible input.
#:
#: FROZEN 2026-09-16 at **10** — the human Ctrl+C experience, set by the user
#: BEFORE AC-S0.10 ran and never derived from the samples it judges.  The
#: measured post-cancel first chunk was median 3.458 s (n=86), so 10 s passes
#: the fleet's healthy adapters; keeping it at 10 rather than widening it is
#: what makes grok (11/15 samples 16-78 s) and codex's queued cell REFUSE
#: ``urgency:"interrupt"`` with a typed reason instead of promising a latency
#: the adapter does not deliver.  Separate from ``ACP_CANCEL_SETTLE_S`` on
#: purpose: a safety bound doubling as a product limit hides the product
#: question (review r8 B1).
INTERRUPT_MAX_LATENCY_S = 10

#: How long a flushed ACP prompt write must survive before the local write
#: receipt counts as "accepted" (D5, D6b(3)).
#:
#: FROZEN 2026-09-16 at **1**.  Fault injection, n=45: the first write after
#: peer death fails between 0.000 s and 0.011 s.  The honest scope, recorded
#: with the number because it bounds what the constant can MEAN: this detects a
#: DEAD peer only.  A wedged LIVE peer produces no local failure at any horizon,
#: so no larger value would buy detection — which is why the row is 1 and not,
#: say, 5.
ACP_WRITE_SETTLE_S = 1

#: How long idempotent process-group teardown waits after ``SIGTERM`` before
#: escalating to ``SIGKILL`` (D6b(3) recovery).
#:
#: FROZEN 2026-09-16 at **3**.  Measured SIGTERM-to-group-exit worst case
#: 0.552 s.  The escalation is MANDATORY rather than defensive: cline never
#: exits on ``SIGTERM`` at all, so a teardown with no ``SIGKILL`` leg would
#: leave a live process group behind the "absence proven" precondition that
#: ``expire_recovery`` requires.
ACP_KILL_GRACE_S = 3

#: The terminal bound on interrupt recovery (close/respawn plus rebind), after
#: which the terminal is retired ``exited{interrupt_recovery_failed}``.
#:
#: FROZEN 2026-09-16 at **120**.  Warm close + respawn + bind measured at most
#: 3.072 s, so 120 s is ~40x the warm path.  It is deliberately NOT stretched to
#: cover a cold ``npx`` resolution: a broken npx cache made ``initialize``
#: exceed 120 s outright, and that is a typed ``exited`` condition plus the
#: AC-S0.7 prefetch fix, not a reason to widen a recovery promise.
RECOVERY_DEADLINE_S = 120

#: The margin between I's persisted cancel-settle deadline and I's own lease
#: expiry, so a cancel settling at the last legal instant is still owned by the
#: claim that issued it (D6b(3): ``lease_until = deadline + this``).
#:
#: FROZEN 2026-09-16 at **15**.  DERIVED: one tick (10) + worst measured settle
#: (0.273) + kill grace (3) = 13.3, rounded up to 15.  The ordering it has to
#: satisfy is ``ACP_CANCEL_SETTLE_S + CANCEL_HOLD_MARGIN_S < DELIVERY_LEASE_S``
#: — 20 + 15 = 35 < 60 — and it is checked below rather than trusted.
CANCEL_HOLD_MARGIN_S = 15

#: The per-TERMINAL gap between two admitted interrupts (D6b(6)).
#:
#: FROZEN 2026-09-16 at **30**.  DERIVED as ``ACP_CANCEL_SETTLE_S +
#: INTERRUPT_MAX_LATENCY_S`` (20 + 10): the previous interrupt's whole worst-case
#: window must close before another may be admitted against the same terminal.
#: Cross-checked against measurement — 30 s is >= 2x the non-grok p95 first
#: chunk of 13.9 s — so a healthy adapter is never rate-limited by it.
INTERRUPT_MIN_GAP_S = 30

#: The per-PRINCIPAL interrupt budget inside ``INTERRUPT_BUDGET_WINDOW_S``
#: (D6b(6)).
#:
#: FROZEN 2026-09-16 at **5**.  Priced, not guessed: a cancelled turn discards a
#: median 33.4k tokens on claude-acp and 18.6k on codex-acp, so five per window
#: caps the waste at roughly 170k tokens per principal per window.  D6b's own
#: counter-argument is that a button which kills work will get pressed; this is
#: the number that bounds the frequency, and it is small on purpose.
INTERRUPT_BUDGET_N = 5

#: The rolling window the per-principal budget is measured over (D6b(6)).
#:
#: FROZEN 2026-09-16 at **600**.  Chosen so N BINDS: at the 30 s terminal gap,
#: 20 gap-spaced interrupts fit inside 600 s, so the budget (5) is the bound
#: that refuses, not the gap.  A window short enough for the gap to dominate
#: would make ``INTERRUPT_BUDGET_N`` decorative.
INTERRUPT_BUDGET_WINDOW_S = 600

#: D7.3's stage-1 cap: the most busy time a row may ever accumulate as lifetime
#: credit, whatever the agent does.
#:
#: NOT one of AC-S1.24's nine.  It is FIXED by I4's equality below rather than
#: chosen: ``DELIVERY_MAX_LIFETIME_S + BUSY_CREDIT_CAP_S + BUSY_CREDIT_MARGIN_S
#: == IDLE_STALL_AGE_S`` and the two outer terms are already fixed (1700, 1800),
#: leaving 100 s to split.  The split is 90/10 — the margin is one
#: ``DELIVERY_TICK_S``, the smallest unit in which the tick can observe a death,
#: and everything else is credit.  A cap is mandatory rather than tidy: an
#: uncapped accumulator lets one ledger error extend a row indefinitely
#: (AC-S1.13's fails-if), and an extension past the cap would push a row's
#: wall-clock age beyond the legacy stall age, which is the #568 non-overlap
#: property I4 exists to hold.
BUSY_CREDIT_CAP_S = 90

#: The slack between the capped busy credit and the legacy stall age, so a row
#: that used its whole credit still dies STRICTLY before the legacy notice could
#: speak about it.  One ``DELIVERY_TICK_S``: the tick has to get one scan in.
BUSY_CREDIT_MARGIN_S = 10

#: How many journal-derived cuts per terminal the bounded ``recent_cuts`` view
#: retains (D6b(4)).  A COUNT, not a duration, and bounded so a replacement seat
#: reading its predecessor's cuts reads a page rather than a history.
CUT_HISTORY_N = 20


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
    * **B1/B2** Seam B's submission wait is bracketed by herdr's own gate below
      and the injection budget above; either bound crossed makes a delivery
      outcome mean something other than what it says.

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
    if HERDR_PROMPT_WAIT_MS <= HERDR_SUBMISSION_GATE_MS:
        # B1 (WP-HERDR Seam B).  A caller bound that expires first answers every
        # stalled submission with the coarser ``timeout`` code, which collapses
        # "herdr watched and saw nothing" into "we stopped watching" — the same
        # AttemptOutcome either way, but the evidence a live round needs to tell
        # a composer-eating provider from a slow one is gone.
        raise ValueError(
            f"B1: HERDR_PROMPT_WAIT_MS ({HERDR_PROMPT_WAIT_MS}) must be > "
            f"HERDR_SUBMISSION_GATE_MS ({HERDR_SUBMISSION_GATE_MS})"
        )
    if HERDR_PROMPT_WAIT_MS > DELIVERY_INJECT_BUDGET_S * 1000:
        # B2.  The injection budget is what the lease was sized to accommodate
        # (I5); a submission wait allowed to exceed it puts one injection outside
        # the bound its own lease was chosen for, and the tick blocks on it.
        raise ValueError(
            f"B2: HERDR_PROMPT_WAIT_MS ({HERDR_PROMPT_WAIT_MS}) must be <= "
            f"DELIVERY_INJECT_BUDGET_S * 1000 ({DELIVERY_INJECT_BUDGET_S * 1000})"
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

    # -----------------------------------------------------------------------
    # WP-ACP-PLANE — the interrupt orderings (AC-S1.24) and I4's extension
    # (AC-S1.17).  A SEPARATE label space from §3's I1-I5 and §A1.4's T1, for
    # the reason those two are separate from each other: three sets of
    # invariants over one constant table collide the moment they share names.
    # -----------------------------------------------------------------------

    # AC-S1.17 clause (2) — SIGN, checked FIRST so each clause has a mutant
    # that names it: a zero margin violates the equality below as well, and
    # whichever check runs first is the one a mutant can be attributed to.  A
    # zero margin lets a fully-credited row die at the same instant the legacy
    # notice speaks about it, which is a tie, not a non-overlap.
    if BUSY_CREDIT_MARGIN_S <= 0:
        raise ValueError(f"I4-margin: BUSY_CREDIT_MARGIN_S ({BUSY_CREDIT_MARGIN_S}) must be > 0")
    if BUSY_CREDIT_CAP_S <= 0:
        raise ValueError(f"I4-cap: BUSY_CREDIT_CAP_S ({BUSY_CREDIT_CAP_S}) must be > 0")
    # AC-S1.17 clause (1) — DRIFT.  Stated as an EQUALITY over LITERALS, which
    # is the whole point: the derived form ("the cap is whatever is left over")
    # could not fail, and r4's blocker was exactly that.  A legacy retune of
    # IDLE_STALL_AGE_S, or a move of DELIVERY_MAX_LIFETIME_S, that does not also
    # move the credit terms now fails the BUILD rather than silently leaving a
    # row able to outlive the legacy stall notice (#568's non-overlap property).
    if DELIVERY_MAX_LIFETIME_S + BUSY_CREDIT_CAP_S + BUSY_CREDIT_MARGIN_S != IDLE_STALL_AGE_S:
        raise ValueError(
            "I4-credit: DELIVERY_MAX_LIFETIME_S + BUSY_CREDIT_CAP_S + "
            "BUSY_CREDIT_MARGIN_S must EQUAL IDLE_STALL_AGE_S, but reads "
            f"{DELIVERY_MAX_LIFETIME_S} + {BUSY_CREDIT_CAP_S} + "
            f"{BUSY_CREDIT_MARGIN_S} = "
            f"{DELIVERY_MAX_LIFETIME_S + BUSY_CREDIT_CAP_S + BUSY_CREDIT_MARGIN_S} "
            f"!= {IDLE_STALL_AGE_S}"
        )

    # AC-S1.24's SEVEN interrupt orderings.
    #
    # U1 the product limit may not exceed the safety bound, or "seen
    #    immediately" would be promised past the point where the cancel is
    #    quarantined and the promise is unowned.
    if INTERRUPT_MAX_LATENCY_S > ACP_CANCEL_SETTLE_S:
        raise ValueError(
            f"U1: INTERRUPT_MAX_LATENCY_S ({INTERRUPT_MAX_LATENCY_S}) must be <= "
            f"ACP_CANCEL_SETTLE_S ({ACP_CANCEL_SETTLE_S})"
        )
    # U2 a settle bound at or past the lease means the claim that issued the
    #    cancel can expire while its own cancel is still legal.
    if ACP_CANCEL_SETTLE_S >= DELIVERY_LEASE_S:
        raise ValueError(
            f"U2: ACP_CANCEL_SETTLE_S ({ACP_CANCEL_SETTLE_S}) must be < "
            f"DELIVERY_LEASE_S ({DELIVERY_LEASE_S})"
        )
    # U3 the WHOLE cancelling reservation — settle plus hold margin, which is
    #    what begin_cancel writes as I's lease_until — has to fit in one lease,
    #    or the reservation outlives the mechanism that grants it.
    if ACP_CANCEL_SETTLE_S + CANCEL_HOLD_MARGIN_S >= DELIVERY_LEASE_S:
        raise ValueError(
            "U3: ACP_CANCEL_SETTLE_S + CANCEL_HOLD_MARGIN_S "
            f"({ACP_CANCEL_SETTLE_S + CANCEL_HOLD_MARGIN_S}) must be < "
            f"DELIVERY_LEASE_S ({DELIVERY_LEASE_S})"
        )
    # U4 the per-terminal gap must cover a whole interrupt window, or a second
    #    interrupt is admissible against a terminal whose first one has not
    #    finished being paid for.
    if INTERRUPT_MIN_GAP_S < ACP_CANCEL_SETTLE_S + INTERRUPT_MAX_LATENCY_S:
        raise ValueError(
            f"U4: INTERRUPT_MIN_GAP_S ({INTERRUPT_MIN_GAP_S}) must be >= "
            "ACP_CANCEL_SETTLE_S + INTERRUPT_MAX_LATENCY_S "
            f"({ACP_CANCEL_SETTLE_S + INTERRUPT_MAX_LATENCY_S})"
        )
    # U5 the budget must BIND: N gap-spaced interrupts have to fit inside the
    #    window, or the per-terminal gap is the only bound and
    #    INTERRUPT_BUDGET_N never refuses anything.
    if INTERRUPT_BUDGET_N * INTERRUPT_MIN_GAP_S > INTERRUPT_BUDGET_WINDOW_S:
        raise ValueError(
            "U5: INTERRUPT_BUDGET_N * INTERRUPT_MIN_GAP_S "
            f"({INTERRUPT_BUDGET_N * INTERRUPT_MIN_GAP_S}) must be <= "
            f"INTERRUPT_BUDGET_WINDOW_S ({INTERRUPT_BUDGET_WINDOW_S})"
        )
    # U6 (added at r18) the local write receipt is charged INSIDE the latency
    #    the product promises; a settle horizon at or past it would make every
    #    accepted prompt a latency breach by construction.
    if ACP_WRITE_SETTLE_S >= INTERRUPT_MAX_LATENCY_S:
        raise ValueError(
            f"U6: ACP_WRITE_SETTLE_S ({ACP_WRITE_SETTLE_S}) must be < "
            f"INTERRUPT_MAX_LATENCY_S ({INTERRUPT_MAX_LATENCY_S})"
        )
    # U7 (added at r18) the recovery bound reserves one post-crash scan plus
    #    process teardown INSIDE the promise, so teardown_at = recovery_deadline
    #    - (DELIVERY_TICK_S + ACP_KILL_GRACE_S) is still in the future when
    #    recovery begins.
    if DELIVERY_TICK_S + ACP_KILL_GRACE_S >= RECOVERY_DEADLINE_S:
        raise ValueError(
            "U7: DELIVERY_TICK_S + ACP_KILL_GRACE_S "
            f"({DELIVERY_TICK_S + ACP_KILL_GRACE_S}) must be < "
            f"RECOVERY_DEADLINE_S ({RECOVERY_DEADLINE_S})"
        )


check_delivery_orderings()
