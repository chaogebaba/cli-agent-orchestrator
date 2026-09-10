"""The gate-round application layer (WP-ARCH Amendment A, slice 2a).

``app/gate`` owns the transactional commands of the gate-round runner (P3): it
decides WHAT happens in a command and in what order, and reaches the store, the
CAS and the git/provider effects only through the ``core.ports`` Protocols the
composition root fills.  ``core/gate`` decides the pure invariants and
transitions beneath it; nothing here imports ``adapters`` (the
``adapters-only-via-composition-root`` contract) and nothing here imports
``services`` (legacy) either.

Slice 2a is the records-and-rendering increment: the commands that write the
§10.2 rows, the renderers that project a brief/ledger/verdict header from those
rows (pins computed, never hand-written), and the re-projection of a whole round
from rows with zero markdown reads.  The question wait adapters and the workflow
script are 2b/2c.
"""

from __future__ import annotations
