"""D6b(6) — principal derivation and the two bounds, as policy.

The store tests exercise the bounds against a real ledger.  These exercise the
half a store test cannot reach: WHO the caller is, and what happens when the
server cannot honestly say.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cli_agent_orchestrator.app.acp.interrupt_limiter import (
    FORBIDDEN_REQUEST_IDENTITY_FIELDS,
    InterruptLimiter,
    SuppliedIdentityRejected,
    ViewerAuthContext,
    mcp_principal,
    reject_supplied_identity,
    viewer_principal,
)
from cli_agent_orchestrator.core.interrupt import (
    CallerPrincipal,
    InterruptRefusal,
    LedgerWindow,
    PrincipalOrigin,
)
from cli_agent_orchestrator.core.timing import (
    INTERRUPT_BUDGET_N,
    INTERRUPT_BUDGET_WINDOW_S,
    INTERRUPT_MIN_GAP_S,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def _window(
    *, admissions: tuple[datetime, ...] = (), last_terminal: datetime | None = None
) -> LedgerWindow:
    return LedgerWindow(principal_admissions=admissions, last_terminal_admission=last_terminal)


def _decide(window: LedgerWindow, *, force: bool = False, now: datetime = NOW):
    return InterruptLimiter().decide(
        principal=CallerPrincipal(origin=PrincipalOrigin.TERMINAL, subject="t1"),
        terminal_id="term-a",
        now=now,
        window=window,
        force=force,
    )


# ------------------------------------------------------------ the two bounds


def test_an_empty_window_admits_with_the_full_budget() -> None:
    decision = _decide(_window())
    assert decision.admitted
    assert decision.quota.remaining_budget == INTERRUPT_BUDGET_N
    assert decision.quota.remaining_gap_s == 0.0


def test_inside_the_terminal_gap_the_refusal_is_rate_limited() -> None:
    decision = _decide(_window(last_terminal=NOW - timedelta(seconds=1)))
    assert decision.refused is InterruptRefusal.RATE_LIMITED
    assert decision.quota.remaining_gap_s == pytest.approx(INTERRUPT_MIN_GAP_S - 1)


def test_the_gap_is_reported_even_on_a_refusal() -> None:
    """A caller told only "no" has to guess whether to retry in a second or in
    ten minutes, and the two bounds have very different answers."""
    decision = _decide(_window(last_terminal=NOW - timedelta(seconds=5)))
    assert decision.refused is not None
    assert decision.quota.remaining_gap_s > 0


def test_the_budget_refuses_once_n_admissions_sit_inside_the_window() -> None:
    admissions = tuple(NOW - timedelta(seconds=index + 1) for index in range(INTERRUPT_BUDGET_N))
    decision = _decide(_window(admissions=admissions))
    assert decision.refused is InterruptRefusal.BUDGET_EXHAUSTED
    assert decision.quota.remaining_budget == 0


def test_admissions_older_than_the_window_do_not_count() -> None:
    """A ROLLING window: an interrupt from an hour ago is not still being paid for."""
    stale = tuple(
        NOW - timedelta(seconds=INTERRUPT_BUDGET_WINDOW_S + index + 1)
        for index in range(INTERRUPT_BUDGET_N + 3)
    )
    decision = _decide(_window(admissions=stale))
    assert decision.admitted
    assert decision.quota.remaining_budget == INTERRUPT_BUDGET_N


def test_the_gap_is_checked_before_the_budget() -> None:
    """Both refusals are free, so the order changes only what the caller is TOLD.

    A caller inside the terminal gap learns the terminal is busy — retry in
    seconds — rather than that its own budget is gone, which is the less specific
    and less actionable of the two facts.
    """
    admissions = tuple(NOW - timedelta(seconds=index + 1) for index in range(INTERRUPT_BUDGET_N))
    decision = _decide(_window(admissions=admissions, last_terminal=NOW - timedelta(seconds=1)))
    assert decision.refused is InterruptRefusal.RATE_LIMITED


def test_force_waives_both_quota_bounds() -> None:
    admissions = tuple(NOW - timedelta(seconds=index + 1) for index in range(INTERRUPT_BUDGET_N))
    decision = _decide(
        _window(admissions=admissions, last_terminal=NOW - timedelta(seconds=1)), force=True
    )
    assert decision.admitted
    # The quota is still REPORTED honestly; force does not pretend it was free.
    assert decision.quota.remaining_budget == 0
    assert decision.quota.remaining_gap_s > 0


def test_the_limiter_defaults_are_the_frozen_literals() -> None:
    """Nothing in the composition root passes a bound; the defaults ARE the freeze."""
    limiter = InterruptLimiter()
    assert limiter._min_gap_s == INTERRUPT_MIN_GAP_S
    assert limiter._budget_n == INTERRUPT_BUDGET_N
    assert limiter._budget_window_s == INTERRUPT_BUDGET_WINDOW_S


# --------------------------------------------------- principals are derived


def test_an_mcp_principal_is_the_terminal_and_the_generation_is_not_in_the_key() -> None:
    first = mcp_principal(terminal_id="term-a", lifecycle_generation=1)
    later = mcp_principal(terminal_id="term-a", lifecycle_generation=9)
    assert first.origin is PrincipalOrigin.TERMINAL
    assert first.budget_key == later.budget_key


def test_a_viewer_with_an_identity_provider_uses_its_oauth_subject() -> None:
    principal = viewer_principal(
        ViewerAuthContext(
            oauth_subject="sub-123",
            bound_to_loopback=False,
            client_is_loopback=False,
            local_installation="chao@box",
        )
    )
    assert isinstance(principal, CallerPrincipal)
    assert principal.subject == "sub-123"


def test_a_loopback_viewer_with_auth_off_gets_the_shared_installation_principal() -> None:
    """Explicitly NOT an authenticated human: attribution is SHARED by everyone
    at that installation, and the design says so rather than inventing a session."""
    principal = viewer_principal(
        ViewerAuthContext(
            oauth_subject=None,
            bound_to_loopback=True,
            client_is_loopback=True,
            local_installation="chao@box",
        )
    )
    assert isinstance(principal, CallerPrincipal)
    assert principal.subject == "local_installation:chao@box"


@pytest.mark.parametrize(
    ("bound", "client"),
    [(False, True), (True, False), (False, False)],
)
def test_a_non_local_viewer_with_auth_off_is_refused_unauthenticated(
    bound: bool, client: bool
) -> None:
    """(g): fails CLOSED the moment "local" stops standing in for "the owner".

    Both halves are checked, and that is deliberate: a reverse proxy or tunnel in
    front of a loopback bind does not make its viewers local, so the bind alone
    is not enough evidence.
    """
    refusal = viewer_principal(
        ViewerAuthContext(
            oauth_subject=None,
            bound_to_loopback=bound,
            client_is_loopback=client,
            local_installation="chao@box",
        )
    )
    assert refusal is InterruptRefusal.UNAUTHENTICATED


def test_the_proxied_opt_in_is_the_only_way_past_the_refusal() -> None:
    principal = viewer_principal(
        ViewerAuthContext(
            oauth_subject=None,
            bound_to_loopback=False,
            client_is_loopback=False,
            local_installation="chao@box",
            proxied_optin=True,
        )
    )
    assert isinstance(principal, CallerPrincipal)


@pytest.mark.parametrize("field", sorted(FORBIDDEN_REQUEST_IDENTITY_FIELDS))
def test_a_request_that_names_its_own_identity_is_rejected(field: str) -> None:
    """(h)'s forgery arm.  REJECTED, not ignored: a field that is silently
    dropped is one callers keep sending and a later refactor eventually reads."""
    with pytest.raises(SuppliedIdentityRejected):
        reject_supplied_identity({field: "anything", "terminal_id": "term-a"})


def test_an_ordinary_request_payload_passes() -> None:
    reject_supplied_identity({"terminal_id": "term-a", "message": "stop"})


def test_the_rejection_covers_objects_as_well_as_mappings() -> None:
    """Two edges hand this two shapes; a check that covered one leaves the other open."""

    class _Request:
        terminal_id = "term-a"
        viewer_id = "forged"

    with pytest.raises(SuppliedIdentityRejected):
        reject_supplied_identity(_Request())
