"""F634 (D15) — the SERVER-PLANE key and the box-plane recovery refusal.

Under F634 D1 a grok box runs a full ``cao-server`` of its own. Two
server-INTERNAL create paths are reached from ``POST /sessions/{name}/recover``
and neither is safe on a box:

* ``epoch_recovery_service`` passes a ``terminal_id`` (id-safe) but threads no
  ``caller_id``, so the recovered lane comes back UNSHIMMED;
* ``provider_rebind_service`` passes NO ``terminal_id`` -- box-side allocation,
  which forks the single id namespace D11's laptop catalog depends on -- and
  preserves ``caller_id``, so the replacement runs the F620 predicate with no
  ``is_box_hosted`` and lands on exit 97.

Wave-1 disposition (D15): box lanes are EXCLUDED from server-side auto-recovery,
keyed on a fact the REFUSING SERVER can itself read -- ``CAO_SERVER_PLANE=box``,
exported into the box server's environment by ``scripts/box-cao-up.sh`` (D2, the
sole wave-1 box-server launcher). The refusal is TYPED and returned to the
CALLER of ``/recover``, who reaps and cold-redispatches.

The blanket is deliberate and its cost is stated in D15: of the two services
only the rebind path is genuinely broken on a box, so refusing the epoch path
too is an over-refusal, traded for one rule instead of two. A process-level key
cannot distinguish the two services' hazards per lane, and a supervisor reap +
cold redispatch is always correct where auto-recovery is merely cheaper.
Narrowing to the rebind path alone is the named follow-on.

Absent the env key the plane is ``laptop`` and every path behaves exactly as
before -- this module adds no behaviour to a laptop server.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

CAO_SERVER_PLANE_ENV = "CAO_SERVER_PLANE"
BOX_PLANE = "box"

#: Structured error code carried by the typed refusal.
BOX_PLANE_RECOVERY_REFUSED_CODE = "box_plane_recovery_refused"


class BoxPlaneRecoveryRefused(Exception):
    """Server-side recovery refused because this server is box-plane.

    Deliberately NOT a ``ValueError``: ``/recover`` maps ``ValueError`` to a
    400 "bad request", and this is not a bad request -- it is a capability the
    serving plane does not offer. It carries its own code so the caller can
    branch on it rather than string-match a message.
    """

    code = BOX_PLANE_RECOVERY_REFUSED_CODE

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"server-side {reason} recovery is refused on a box-plane cao-server "
            f"({CAO_SERVER_PLANE_ENV}={BOX_PLANE}); reap the lane and cold-redispatch "
            "it from the supervisor"
        )

    def detail(self) -> dict[str, str]:
        """Structured HTTP detail body for the typed refusal."""
        return {"code": self.code, "reason": self.reason, "message": str(self)}


def server_plane(env: Optional[Mapping[str, str]] = None) -> str:
    """The plane this server process runs on: ``box`` or ``laptop``.

    Read from the process environment (or *env*, a test seam). Any value other
    than ``box`` -- including the key being absent, which is the laptop case --
    resolves to ``laptop``, so a typo can never silently disarm a laptop
    server's recovery.
    """
    source = os.environ if env is None else env
    return BOX_PLANE if source.get(CAO_SERVER_PLANE_ENV, "").strip() == BOX_PLANE else "laptop"


def is_box_plane(env: Optional[Mapping[str, str]] = None) -> bool:
    """True iff this server process is the box-plane ``cao-server`` (D15)."""
    return server_plane(env) == BOX_PLANE


def refuse_recovery_on_box_plane(reason: str, env: Optional[Mapping[str, str]] = None) -> None:
    """Raise :class:`BoxPlaneRecoveryRefused` when the serving process is box-plane.

    Called at the ENTRY of each recover service rather than at the route, so
    the refusal holds for any future caller of those services and no
    replacement terminal is created before it fires.
    """
    if is_box_plane(env):
        raise BoxPlaneRecoveryRefused(reason)
