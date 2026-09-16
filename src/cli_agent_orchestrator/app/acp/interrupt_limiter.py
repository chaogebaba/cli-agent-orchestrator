"""D6b(6) — ONE interrupt limiter: two bounds, server-derived principals, refused
at the edge.

The counter-argument D6b states and does not dodge: giving a supervisor a button
that kills work will get it pressed.  An LLM supervisor's judgement of "urgent
enough" is calibrated by nothing, it cannot see what the worker is ninety percent
through, and another lane pays for a wrong call.  This module does not fix that.
It bounds the FREQUENCY, so over-use is measurable and answerable with policy
later; the mitigation for judgement is visibility, and that lives in the journal.

Three properties, each of which is a named AC-S1.22 arm:

* **The budget key is ``(origin, subject)`` and nothing else.**  Not the
  attach-session id, not the lifecycle generation, not anything a request
  carries.  A viewer that re-attaches five times and restarts once inside the
  window has the same budget it started with (arm (h)), because none of those
  events change either component.

* **Quota is charged only after the reservation CAS.**  The caller is the store,
  which CASes ``none -> pending`` first and only then asks this module.  A
  refused reservation therefore costs nothing, which is what makes
  ``INTERRUPT_IN_PROGRESS`` a free refusal (r14, review r13 N2).

* **``force`` waives the two quota bounds and NOTHING else.**  It is a viewer-only
  escape for a human who has decided the interrupt is worth the tokens.  It never
  reaches the reservation CAS, so a forced interrupt still cannot enter
  ``cancelling`` while another interrupt owns the terminal (arm (f)).

Principal derivation lives here too, and it is the security half.  ``CallerPrincipal``
is ISSUED BY THE SERVER.  A request that carries ``viewer_id``,
``attach_session_id`` or ``principal`` is rejected outright rather than having
those fields ignored, because a field that is silently dropped is one a caller
will keep sending and a future refactor will eventually read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from cli_agent_orchestrator.core.interrupt import (
    CallerPrincipal,
    InterruptRefusal,
    LedgerWindow,
    LimiterDecision,
    PrincipalOrigin,
    Quota,
)
from cli_agent_orchestrator.core.timing import (
    INTERRUPT_BUDGET_N,
    INTERRUPT_BUDGET_WINDOW_S,
    INTERRUPT_MIN_GAP_S,
)

__all__ = [
    "FORBIDDEN_REQUEST_IDENTITY_FIELDS",
    "InterruptLimiter",
    "SuppliedIdentityRejected",
    "ViewerAuthContext",
    "mcp_principal",
    "viewer_principal",
]


#: Identity a request may NEVER supply.  Rejected, not ignored: an ignored field
#: is one callers keep sending and a later refactor eventually reads.
FORBIDDEN_REQUEST_IDENTITY_FIELDS: frozenset[str] = frozenset(
    {"principal", "viewer_id", "attach_session_id", "subject", "origin"}
)


class SuppliedIdentityRejected(ValueError):
    """A request tried to name its own principal.  AC-S1.22(h)'s forgery arm."""


@dataclass(frozen=True)
class ViewerAuthContext:
    """What the server knows about a viewer request at the moment it arrives.

    Every field is server-observed.  ``oauth_subject`` is populated only when
    ``security/auth.py`` has an identity provider configured; the rest describe
    the deployment, and together they decide whether the shared local-installation
    principal may be used at all.
    """

    oauth_subject: str | None
    bound_to_loopback: bool
    client_is_loopback: bool
    local_installation: str
    proxied_optin: bool = False


def mcp_principal(*, terminal_id: str, lifecycle_generation: int) -> CallerPrincipal:
    """The MCP caller's principal, derived from the VERIFIED D22 token.

    The generation rides along for attribution and is deliberately absent from
    ``budget_key``: a terminal whose lifecycle generation bumped is the same
    principal spending the same budget, and letting a respawn refresh quota would
    hand every caller a reset button.
    """
    return CallerPrincipal(
        origin=PrincipalOrigin.TERMINAL,
        subject=terminal_id,
        lifecycle_generation=lifecycle_generation,
    )


def viewer_principal(
    context: ViewerAuthContext, *, attach_session_id: str | None = None
) -> CallerPrincipal | InterruptRefusal:
    """The viewer's principal, or ``INTERRUPT_UNAUTHENTICATED``.

    Auth is default-off in this product: ``security/auth.py`` grants every path
    the full scope set in that mode, so there is no authenticated session to
    derive a subject from.  The design says so instead of inventing one.  What it
    uses instead is the **shared local-installation principal** — stable,
    non-secret, and explicitly NOT an authenticated human: attribution is shared
    by everyone at that installation.

    It fails CLOSED the moment "local" stops being a safe stand-in for "the
    person who owns this machine": a non-loopback bind, or a request whose client
    address is not loopback.  A reverse proxy or tunnel in front of a loopback
    bind does not make its viewers local, which is why the client address is
    checked as well as the bind.  An operator who genuinely wants proxied
    auth-off viewers opts in explicitly, and that opt-in is journalled on every
    admission it permits (r12, review r11 N1).
    """
    if context.oauth_subject:
        return CallerPrincipal(
            origin=PrincipalOrigin.VIEWER,
            subject=context.oauth_subject,
            attach_session_id=attach_session_id,
        )
    local_is_safe = context.bound_to_loopback and context.client_is_loopback
    if not local_is_safe and not context.proxied_optin:
        return InterruptRefusal.UNAUTHENTICATED
    return CallerPrincipal(
        origin=PrincipalOrigin.VIEWER,
        subject=f"local_installation:{context.local_installation}",
        attach_session_id=attach_session_id,
    )


def reject_supplied_identity(payload: object) -> None:
    """Raise if a request payload names its own identity.

    Takes ``object`` and checks both mappings and attribute-bearing objects,
    because the two edges hand this two different shapes and a check that only
    covered one would leave the other open.
    """
    present: set[str]
    if isinstance(payload, dict):
        present = set(FORBIDDEN_REQUEST_IDENTITY_FIELDS & set(payload))
    else:
        present = {field for field in FORBIDDEN_REQUEST_IDENTITY_FIELDS if hasattr(payload, field)}
    if present:
        raise SuppliedIdentityRejected(
            "identity is issued by the server, never supplied by a request; "
            f"rejected field(s): {sorted(present)}"
        )


class InterruptLimiter:
    """The single authority for D6b(6)'s two bounds.

    Stateless by construction.  Every number it needs arrives in the
    :class:`~cli_agent_orchestrator.core.interrupt.LedgerWindow` the store read
    inside its own transaction, so there is no in-process counter that a restart
    could reset or that two workers could disagree about.  That is what makes
    AC-S1.22(e) — "the ledger survives a ``cao-server`` restart mid-window" —
    a property of the design rather than of a cache's lifetime.

    The bounds are checked GAP FIRST.  Both refusals are free, so the order does
    not change what is charged, but it changes what the caller is TOLD: a caller
    inside the terminal gap learns the terminal is busy (retry in seconds) rather
    than that its own budget is gone (retry in minutes), and the gap is the more
    specific fact.
    """

    def __init__(
        self,
        *,
        min_gap_s: int = INTERRUPT_MIN_GAP_S,
        budget_n: int = INTERRUPT_BUDGET_N,
        budget_window_s: int = INTERRUPT_BUDGET_WINDOW_S,
    ) -> None:
        # The defaults are AC-S1.24's frozen literals.  They are constructor
        # arguments only so a test can state a bound it is exercising; nothing in
        # the composition root passes anything but the defaults, and a test
        # asserts that.
        self._min_gap_s = min_gap_s
        self._budget_n = budget_n
        self._budget_window_s = budget_window_s

    def decide(
        self,
        *,
        principal: CallerPrincipal,
        terminal_id: str,
        now: datetime,
        window: LedgerWindow,
        force: bool,
    ) -> LimiterDecision:
        """Admit or refuse, reporting the quota either way."""
        del terminal_id  # the window is already scoped to it; named for the port's shape
        del principal  # likewise — the window is this principal's
        horizon = now - timedelta(seconds=self._budget_window_s)
        inside = [stamp for stamp in window.principal_admissions if stamp > horizon]
        remaining_budget = max(0, self._budget_n - len(inside))

        remaining_gap_s = 0.0
        if window.last_terminal_admission is not None:
            elapsed = (now - window.last_terminal_admission).total_seconds()
            remaining_gap_s = max(0.0, self._min_gap_s - elapsed)

        quota = Quota(remaining_gap_s=remaining_gap_s, remaining_budget=remaining_budget)
        if force:
            # The two QUOTA bounds, and only those.  The reservation CAS already
            # ran in the caller and force never reaches it.
            return LimiterDecision(refused=None, quota=quota)
        if remaining_gap_s > 0:
            return LimiterDecision(refused=InterruptRefusal.RATE_LIMITED, quota=quota)
        if remaining_budget <= 0:
            return LimiterDecision(refused=InterruptRefusal.BUDGET_EXHAUSTED, quota=quota)
        return LimiterDecision(refused=None, quota=quota)
