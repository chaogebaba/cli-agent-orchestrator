"""What a boot switch answers when a position it once accepted is gone (#738).

The strangler phases each own a switch — ``CAO_WORKER_TRUTH_INGEST``,
``CAO_WORKER_TRUTH_STATUS``, ``CAO_DELIVERY_QUEUE`` — and two of them shipped a
``shadow`` position: the new machinery runs, nothing is served from it, and the
evidence for a flip comes from a dark deployment running beside the real one.
That mode is RETIRED (user ruling 2026-09-09, #738).  A flag flip is accepted by
a grok-box live round now.

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

from dataclasses import dataclass

__all__ = [
    "Rejected",
    "retired_position",
]


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
