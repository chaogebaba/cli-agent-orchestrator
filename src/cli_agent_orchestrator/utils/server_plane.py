"""F634 (#489) D15: server-plane identity and the box-plane recovery refusal.

A box-hosted ``cao-server`` is launched by D2's ``box-cao-up.sh`` with
``CAO_SERVER_PLANE=box`` in its environment. D15's recover disposition
EXCLUDES box lanes from server-side auto-recovery, keyed on that
SERVER-PLANE fact the refusing process can actually read: both recover
services (``epoch_recovery_service``, ``provider_rebind_service``) refuse when
the serving process is box-plane, returning a TYPED refusal to the CALLER of
``/recover`` (who reaps and cold-redispatches — the capped-lane default).

The blueprint records this as a DELIBERATE OVER-refusal: of the two recover
services only ``provider_rebind_service`` is genuinely broken on a box (it
allocates box-side, forking D11's id namespace, and preserves ``caller_id`` so
the replacement hits exit 97); the epoch path is id-safe. Wave 1 accepts one
blanket rule over two per-lane rules on purpose — the plane key is
process-level and cannot distinguish the two services' hazards per lane, and a
supervisor reap + cold redispatch is always correct where an auto-recovery is
merely cheaper. Narrowing the refusal to the rebind path alone is the named
follow-on; the blanket is implemented AS SPECIFIED here, not narrowed.
"""

from __future__ import annotations

import os
from typing import Dict

_SERVER_PLANE_ENV = "CAO_SERVER_PLANE"
_BOX_PLANE = "box"


def server_plane() -> str:
    """Return the serving process's plane, lowercased and stripped.

    Reads ``CAO_SERVER_PLANE`` from the process environment. Absent/blank →
    ``""`` (a laptop/default server), so laptop behaviour is byte-identical to
    before F634: nothing exports the key on a laptop.
    """
    return os.environ.get(_SERVER_PLANE_ENV, "").strip().lower()


def is_box_plane() -> bool:
    """True iff the serving process was launched with ``CAO_SERVER_PLANE=box``."""
    return server_plane() == _BOX_PLANE


class BoxPlaneRecoveryRefused(RuntimeError):
    """F634 (#489) D15: a recover was requested on a box-plane ``cao-server``.

    Box lanes are excluded from server-side auto-recovery. The refusal is a
    TYPED response the caller of ``/recover`` receives and relays — NO
    replacement terminal is created — so the caller reaps the original id
    (still resolvable in the laptop catalog) and cold-redispatches. The
    ``/recover`` route maps this to a 409 whose ``detail`` carries
    ``code="E-BOX-PLANE-NO-RECOVER"`` plus the refused reason.
    """

    code = "E-BOX-PLANE-NO-RECOVER"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"server-side recovery is refused on a box-plane cao-server "
            f"(CAO_SERVER_PLANE=box): {reason}. Reap the lane and cold-redispatch."
        )

    def detail(self) -> Dict[str, str]:
        """Structured HTTP detail body naming the refused reason."""
        return {
            "code": self.code,
            "message": str(self),
            "reason": self.reason,
        }


def refuse_recovery_if_box_plane(reason: str) -> None:
    """Raise ``BoxPlaneRecoveryRefused`` when the serving process is box-plane.

    A no-op on a laptop/default server (``CAO_SERVER_PLANE`` unset), so the
    laptop recovery path is unchanged. Both recover services call this at
    their entry point BEFORE selecting or creating any terminal, so a box-plane
    refusal creates no replacement terminal.
    """
    if is_box_plane():
        raise BoxPlaneRecoveryRefused(reason)
