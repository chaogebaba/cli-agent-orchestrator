"""What a boot switch answers when a position it once accepted is gone (#738).

The strangler phases each own a switch — ``CAO_WORKER_TRUTH_INGEST`` and
``CAO_WORKER_TRUTH_STATUS`` — and both shipped a ``shadow`` position: the new
machinery runs, nothing is served from it, and the evidence for a flip comes from
a dark deployment running beside the real one.  That mode is RETIRED (user ruling
2026-09-09, #738).  A flag flip is accepted by a grok-box live round now.

``CAO_DELIVERY_QUEUE`` was a third, and WP-ARCH 3c slice 4 removed it entirely: a
switch chooses between two carriers, and after 3c there is only one.  This module
outlives it because the other two switches still need what it does.

Retiring a position that shipped is not the same as never having had it, and this
module is that difference.  An operator still carrying ``=shadow`` in a systemd
drop-in typed something that USED to work, so folding it into each parser's
unknown-value default would start their deployment in ``off`` and say nothing —
the mode would appear to be running while nothing ran.  :class:`Rejected` is the
answer instead: it names the value, cites the retirement, and carries the literal
line to type.

It is a VALUE, not an exception, because these switches are read in the
composition root of a server whose boot must not be self-inflicted-failed by a
diagnosability feature.  The caller declines to start that ONE subsystem and logs
:attr:`Rejected.detail`; the server still boots.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "BOOT_SWITCH_ON",
    "Rejected",
    "boot_switch_enabled",
    "retired_position",
]

#: The ONE spelling of "on" for a two-position boot switch.
#:
#: ``CAO_WORKER_TRUTH_INGEST`` chose it and stated why: a switch that also
#: accepted ``"true"``, ``"yes"`` and ``"on"`` would be a switch nobody could
#: state the position of from a process listing.  Every later phase repeats that
#: choice, so it is written down once here instead of being re-argued per phase.
BOOT_SWITCH_ON = "1"


def boot_switch_enabled(env_var: str, source: Mapping[str, str]) -> bool:
    """Is this two-position boot switch ON in ``source``?

    The post-3c idiom for a strangler phase's own switch, in one function so the
    next phase is a two-line addition rather than a fourth hand-rolled parser.
    Three properties, and each is a decision an earlier phase made the hard way:

    * **Default OFF.**  An absent variable is ``False``, so merging a phase
      leaves the branch inert and ``main`` byte-identical in behaviour until a
      grok-box live round says otherwise (#738: the flag flip IS the acceptance,
      there is no shadow phase).
    * **Strictly ``"1"``.**  See :data:`BOOT_SWITCH_ON`.
    * **PURE.**  It takes the mapping rather than reading ``os.environ``, which
      is what lets it live in ``core`` at all and what lets a test state a
      position without mutating process state.

    Phases whose switch has MORE than two positions do not use this: they own a
    parser that can answer :class:`Rejected` for a position that shipped and was
    retired.  A two-position switch has no retired position to refuse, because
    it never had a third.
    """
    return source.get(env_var) == BOOT_SWITCH_ON


@dataclass(frozen=True)
class Rejected:
    """A requested switch position this build refuses to resolve.

    ``value`` is what the operator actually typed (already normalised), ``reason``
    says why it is gone, and ``hint`` is the fix — a literal assignment they can
    paste, never a description of one.
    """

    value: str
    reason: str
    hint: str

    @property
    def detail(self) -> str:
        return f"{self.reason}; {self.hint}"


def retired_position(*, env_var: str, value: str, accepted: str) -> Rejected:
    """The standard rejection for a position removed by #738.

    One phrasing for every switch, so an operator who has seen the delivery
    queue's refusal recognises the status cutover's without re-reading it.
    """
    return Rejected(
        value=value,
        reason=(
            f"{env_var}={value!r} is retired: shadow-live mode was removed (#738), "
            "so nothing would serve this deployment"
        ),
        hint=f"set {env_var}={accepted}",
    )
