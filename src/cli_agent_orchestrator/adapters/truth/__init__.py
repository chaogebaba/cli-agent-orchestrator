"""Phase-1 truth producers (WP-ARCH F725 #581, blueprint §4 AC4/AC4b/AC4c).

Four producers write into the one append-only ``worker_event`` log, and the
blueprint's precedence rule (U6, r9) is a property of the SOURCE, not of an
individual row:

* :mod:`.codex_rollout` — tails the codex rollout JSONL.  The only
  ``authoritative`` producer in phase 1; JSONL-first is decision U6 and hooks are
  the phase-2 accelerator, not the truth.
* :mod:`.legacy_egress` — one line at ``StatusMonitor._publish_observation``,
  the single egress every status origin passes through.  ``derived``: it reports
  what the pane classifier concluded, which is exactly the thing this work
  package exists to stop trusting blindly.
* :mod:`.liveness_probe` — one ``tmux list-panes -a`` per tick for the whole
  fleet.  Heartbeats land in projection COLUMNS; only edges become rows.  Sole
  owner of ``process.exited``.
* :mod:`.server_decisions` — what the SERVER did, recorded beside the
  observation that justified it: ``delivery.attempt``, ``teardown.decided``,
  ``teardown.intended``, ``fleet.override``.

Every one of them constructs an :class:`~cli_agent_orchestrator.core.events.EventDraft`
and hands it to :func:`.wiring.emit`.  None of them mints an ``event_id`` or a
``seq`` — the store does that, inside the single transaction that also bumps the
per-terminal high-water mark, because a producer that could choose its own ``seq``
is a producer that can leave a gap (B7).

Nothing here imports ``app``: adapters are leaves.
"""
