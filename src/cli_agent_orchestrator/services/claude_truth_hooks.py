"""The one legacy file that names the new tree for phase 2's claude_code hooks.

**Why this file exists at all, since the blueprint said ``api/main.py``.**  D3b
places phase 2's hook producer "in those route handlers", and §5 accordingly lists
``api/main.py`` as the seventh entry of the AC11 importer set.  Built that way it
does not compile past the linter: the fifth import-linter contract,
``adapters-only-via-composition-root``, forbids ``cli_agent_orchestrator.api``
from importing ``cli_agent_orchestrator.adapters`` at all — the composition root
is the only module allowed to name an adapter.  The blueprint's decision is right
about WHERE the append belongs; it is the import that has nowhere to live.

So the append happens where D3b says — inside the two route handlers — and the
handlers reach the producer through this shim, which is legacy and may therefore
import the new tree.  That is not an invention: it is the pattern the fork has
used twice already for exactly this reason.  Lane C put the diag CLI's new-tree
imports in ``cli/commands/diag.py`` and left ``cli/main.py`` importing only that
module; WP-ARCH phase 3a put five delivery hook points behind
``services/delivery_mirror.py`` for the same purpose, so that "the contact surface
a reviewer has to read stays at one file instead of spreading across the two
largest legacy packages".  This is the third application, and the AC11 set grows
by one file as §5 says it must — ``services/claude_truth_hooks.py`` rather than
``api/main.py``.

Everything here is a thin forward.  No decision, no state, no error handling of
its own: the producers already promise never to raise, and a shim that added a
second guarantee would be a second place to look when the first one broke.
"""

from __future__ import annotations

from cli_agent_orchestrator.adapters.truth import claude_hooks as _wt_claude_hooks
from cli_agent_orchestrator.adapters.truth import claude_transcript as _wt_claude_transcript

__all__ = [
    "attach_transcript_source",
    "detach_transcript_source",
    "record_interaction_marker",
]


def attach_transcript_source(terminal_id: str, transcript_path: str, session_id: str) -> None:
    """Hand a transcript binding epoch to the tailer (D3, §5).

    The path is HANDED IN and never resolved here (parent N2): the route has
    already validated that it resolves under the provider home, and a second
    resolution would be a second implementation of the identity rules.
    """
    _wt_claude_transcript.attach(terminal_id, transcript_path, session_id)


def detach_transcript_source(terminal_id: str) -> None:
    """Stop tailing one terminal.  The path's cursor is kept — see B5."""
    _wt_claude_transcript.detach(terminal_id)


def record_interaction_marker(
    terminal_id: str,
    marker_kind: str,
    *,
    hook_event: str = "",
    tool_name: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    """Append the ``prompt.awaiting``/``prompt.answered`` edge for one marker POST."""
    _wt_claude_hooks.record_interaction_marker(
        terminal_id,
        marker_kind,
        hook_event=hook_event,
        tool_name=tool_name,
        idempotency_key=idempotency_key,
    )
