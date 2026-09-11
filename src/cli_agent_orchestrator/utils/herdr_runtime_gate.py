"""The H1 runtime switch and the certified-cohort predicate (WP-HERDR §6, §8, D9).

Two questions live here, and they are deliberately separate:

1. **Is the herdr runtime seam armed at all?**  ``CAO_HERDR_RUNTIME`` — read
   here, in one module, and nowhere else.  It defaults OFF so that merging H1
   leaves ``main`` byte-identical in behaviour until a grok-box live round says
   otherwise (#738: the flag flip IS the acceptance; there is no shadow phase).
2. **Is THIS terminal a certified herdr cohort member?**  D9's
   ``herdr_certification:`` record, resolved through
   :func:`~cli_agent_orchestrator.utils.routing.herdr_cell_certified` for the
   terminal's bound (position, provider) cell.

The blueprint's §8 fallback rule is why the second answer is CACHED per terminal
rather than recomputed: *never switch truth sources mid-occupant*.  A position
file edited while a worker is running must not silently move that worker from
the herdr lifecycle source back onto the scraped pane halfway through a turn —
the cohort would flip truth sources under a live occupant, which is exactly the
class of surprise §8 forbids.  So the answer is resolved once per terminal per
process and held until the terminal is forgotten at teardown.

**A cao-server restart forgets every binding, and that is the safe direction.**
The map is process-local, so terminals that survive a restart come back UNBOUND
and therefore UNCERTIFIED until something re-binds them. The consequence is
stated rather than hidden: after a restart a live certified terminal loses its
§6(ii) mute and its certified-path gates, i.e. it reverts to the pre-H1 scraped
lifecycle — the pane fallback it had before, never a state derived from nothing.
That is the correct failure direction for a predicate whose whole job is deciding
whom to believe, and it is why the answer is not persisted: a durable row would
survive into a process whose backend, herdr binary or position file may all have
changed, and §8's "never switch truth sources mid-occupant" would then be
enforced against a fact nobody re-checked. Re-binding a surviving occupant at
startup is a separate decision and needs its own evidence (H1 report, §8 note).

This module is LEGACY (``utils/``) on purpose.  The predicate is read from three
legacy callers — the herdr backend shim, the pi provider and the stalled
callback watchdog — and from the composition-root side of the projector wiring;
putting it in ``core``/``app`` would make every one of those a new-tree importer
and blow the AC11 contact surface open for a boolean.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "HERDR_RUNTIME_ENV_VAR",
    "bind_terminal",
    "forget_terminal",
    "herdr_runtime_enabled",
    "herdr_lifecycle_authoritative",
    "reset_gate",
    "terminal_certified",
]

#: The one spelling of the H1 switch.  Same discipline as
#: ``adapters/truth/wiring``'s note on ``CAO_WORKER_TRUTH_INGEST``: a second
#: definition in another module is how a switch starts meaning two things.
HERDR_RUNTIME_ENV_VAR = "CAO_HERDR_RUNTIME"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_lock = threading.RLock()
#: CAO terminal id -> its resolved certification answer, decided once.
_certified: Dict[str, bool] = {}


def herdr_runtime_enabled() -> bool:
    """Is the H1 herdr runtime seam armed for this process?

    Read per call rather than cached at import: the server reads it at terminal
    create, and a test that sets the variable must not have to reload a module.
    """
    return os.environ.get(HERDR_RUNTIME_ENV_VAR, "").strip().lower() in _TRUTHY


def bind_terminal(terminal_id: str, certified: bool) -> None:
    """Record this terminal's certification answer for the life of the occupant.

    Called once, at terminal create, from the seam that already resolved the
    (position, provider) cell.  Idempotent for the same answer; a SECOND, different
    answer for a live terminal is refused and logged rather than applied — that
    would be the mid-occupant source switch §8 forbids.
    """
    if not terminal_id:
        return
    with _lock:
        previous = _certified.get(terminal_id)
        if previous is not None and previous != certified:
            logger.warning(
                "herdr certification for terminal %s would change %s->%s mid-occupant; "
                "keeping the bound answer (blueprint §8: never switch truth sources "
                "under a live occupant)",
                terminal_id,
                previous,
                certified,
            )
            return
        _certified[terminal_id] = certified


def forget_terminal(terminal_id: str) -> None:
    """Drop a terminal's answer at teardown, so a reused id re-resolves."""
    with _lock:
        _certified.pop(terminal_id, None)


def terminal_certified(terminal_id: str) -> bool:
    """Is this terminal's bound cell certified for the herdr backend?

    ``False`` for a terminal nobody bound — which is every terminal on the tmux
    backend, every terminal created before the flag was armed, and every terminal
    whose cell has no PASS ``herdr_certification`` row.  Fail-closed is the right
    default here: an unbound terminal keeps the scraped pane as its lifecycle
    source, which is the pre-H1 behaviour.
    """
    with _lock:
        return _certified.get(terminal_id, False)


def herdr_lifecycle_authoritative(terminal_id: Optional[str]) -> bool:
    """The single predicate the certified-path branches read.

    Both halves must hold: the seam must be ARMED (so ``main`` is inert by
    default) and the terminal's cell must be CERTIFIED (so certification, not the
    backend setting, is what switches a terminal over — §1 of the H1 plan).
    """
    if not terminal_id:
        return False
    if not herdr_runtime_enabled():
        return False
    return terminal_certified(terminal_id)


def reset_gate() -> None:
    """Drop every bound answer.  For tests and a re-installed bootstrap."""
    with _lock:
        _certified.clear()
