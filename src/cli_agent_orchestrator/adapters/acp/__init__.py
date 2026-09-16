"""The ACP wire adapter (WP-ACP-PLANE D3).

**Hand-rolled newline-delimited JSON-RPC, not the Python SDK.**  D3 settles that
and AC-S0.6 closed the re-check: ``agent-client-protocol`` 0.12.1 and 1.0.0rc1
both pin ``PROTOCOL_VERSION = 1``, neither exposes steering, and the earlier
"import acp fails" reading was a not-installed artefact.  A dependency that adds
no capability and pins the same protocol version buys nothing and costs a
release cadence we do not control.

This package owns BYTES and nothing else (D6b(3)): framing, stream
demultiplexing and the runtime active-turn handle.  It receives no store, writes
no journal and no queue row, and returns typed events the application layer
records.  That split is what makes the scheduler's isolation provable — a port
that could write would have to be awaited inside a transaction.

AC-S1.9 greps this package for gate, review, verdict, digest and
certification-policy identifiers and fails on any of them.  The plane is
infrastructure; it knows terminals, envelopes and transport outcomes.
"""

from __future__ import annotations

__all__: list[str] = []
