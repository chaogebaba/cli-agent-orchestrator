"""The ACP message plane's application layer (WP-ACP-PLANE).

Infrastructure only.  D0 is explicit about what may not appear here: this
package knows terminals, envelopes, conditions, transport outcomes and the
routing table, and has **zero** knowledge of gates, reviews, verdicts, digests or
certification-as-routing-input policy.  AC-S1.9 greps this directory for those
identifiers, and the plane's tests pass with the ``/orchestrator`` skill absent.
"""

from __future__ import annotations

__all__: list[str] = []
