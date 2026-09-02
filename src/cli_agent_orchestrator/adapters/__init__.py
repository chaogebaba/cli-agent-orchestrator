"""WP-ARCH phase 1 (F725 #581) — the outward edge of the strangler tree.

``adapters`` is where the new tree touches the world: SQLite, tmux, the codex
rollout JSONL, and the legacy fork code that has not moved yet.  Two layering
rules from the audit §2.3 shape everything here:

* **adapters are leaves.**  Nothing under ``adapters`` may import ``app``.  A
  producer therefore never calls the projector; it appends to the event store and
  the projector reads from there.  That is what keeps the truth path one-way.
* **only the composition root names an adapter.**  ``app``, ``api``,
  ``mcp_server`` and ``cli`` may not import this package at all; ``bootstrap.py``
  builds the instances and passes them on as ``core.ports`` Protocols.

The one deliberate exception is the legacy tree.  The seven AC11 hook points live
in ``services/`` and ``providers/`` and call into ``adapters.truth`` directly,
because a legacy module has no composition root to be injected from.  Those calls
are no-ops unless ``bootstrap.py`` has installed a runtime — see
``adapters/truth/wiring.py``.
"""
