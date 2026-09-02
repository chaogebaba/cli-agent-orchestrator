"""Every phase-1 duration, in one place (WP-ARCH blueprint §4c).

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
    "NO_SIGNAL_S",
    "PANE_HEARTBEAT_S",
    "PANE_MISS_TICKS",
    "PROBE_FAIL_TICKS",
    "RETENTION_DAYS",
    "RETENTION_SWEEP_S",
    "ROLLOUT_POLL_MS",
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
