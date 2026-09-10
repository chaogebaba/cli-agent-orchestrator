"""The delivery application layer (WP-ARCH phase 3).

Sub-phase 3a built an observational half beside it — a copied row per legacy
insert, a mirror writer, an agreement report — and that whole mode is RETIRED
(#738, user ruling 2026-09-09).  A flag flip is accepted by a grok-box live round
now, not by a dark deployment.  What remains is the real thing: the queue, its
tick, its seat wake, and the write-through that makes it the authority.

The layering rule this package lives under: ``app`` may not import ``adapters``
and may not import legacy.  It reaches the store through
``core.ports.QueueStore`` and it receives legacy facts as plain values through
:mod:`~app.delivery.wiring`, handed in by the one legacy module that knows both
halves.
"""

from __future__ import annotations
