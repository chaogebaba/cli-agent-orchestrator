"""WP-ARCH phase 1 (F725 #581) — adapters implementing ``core.ports``.

Adapters are LEAVES.  They import ``core`` and the standard library, and nothing
else in the tree: not ``app`` (the ``adapters-are-leaves`` contract), not
``services``/``clients``/``utils``/``providers`` (the
``new-code-never-imports-legacy`` contract), and notably not ``constants`` —
every path and tunable an adapter needs is HANDED IN by
``cli_agent_orchestrator/bootstrap.py``, the one module allowed to name both
sides.

That last rule is what keeps a test able to point the store at a scratch file
without a monkeypatch, and what keeps the legacy configuration sprawl from
growing a new consumer while phase 6 is still ahead of us.

Traffic runs one way through here.  ``adapters/store/`` implements the event log,
the finding table and retention; ``adapters/truth/`` holds the four phase-1
PRODUCERS, which append to the store and never call the projector — that is what
``adapters-are-leaves`` buys.

The one place the arrow points inward from outside is the legacy tree.  The seven
AC11 hook points live in ``services/`` and ``providers/`` and call into
``adapters.truth`` directly, because a legacy module has no composition root to be
injected from.  Every one of those calls is a no-op until ``bootstrap.py`` has
armed ingestion — see ``adapters/truth/wiring.py``.
"""
