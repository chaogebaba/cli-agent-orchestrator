"""The gate store port, re-exported for ``app/gate`` (WP-ARCH Amendment A, 2a).

The blueprint (§10.3) names ``app/gate/ports.py`` as the module that declares the
store Protocol.  The Protocol and the value types it returns actually LIVE in
``core`` — ``core.ports.GateStore`` and ``core.gate.RoundProjection`` /
``ClaimOwnershipResult`` — because the store adapter returns those shapes and
``adapters-are-leaves`` forbids ``adapters`` from importing ``app``.  A value an
adapter hands back is core vocabulary, exactly as ``QueueStore`` and
``StateProjection`` are (``core/ports.py``).

This module is the ``app``-side name for that Protocol: ``app/gate`` imports the
port from here, so the blueprint's module exists and application code reads
``from cli_agent_orchestrator.app.gate.ports import GateStore`` while the single
definition stays in ``core``.  Re-export, not redefinition — one Protocol, one
authority.
"""

from __future__ import annotations

from cli_agent_orchestrator.core.gate import ClaimOwnershipResult, RoundProjection
from cli_agent_orchestrator.core.ports import GateStore

__all__ = ["ClaimOwnershipResult", "GateStore", "RoundProjection"]
