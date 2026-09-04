"""The delivery application layer (WP-ARCH phase 3).

Sub-phase 3a builds the observational half: a shadow row written beside each
legacy ``send_message`` insert, a mirror writer that advances that row from what
the legacy path actually did, and the agreement report that compares the two.
Nothing here delivers anything — ``claim`` has a ``mode='live'`` filter and every
row this package writes is ``mode='shadow'``.

The layering rule this package lives under: ``app`` may not import ``adapters``
and may not import legacy.  It reaches the store through
``core.ports.QueueStore`` and it receives legacy facts as plain values through
:mod:`~app.delivery.wiring`, handed in by the one legacy module that knows both
halves.  That is what keeps the comparison honest, too — this package cannot
peek at the inbox table to make its own side agree.
"""

from __future__ import annotations
