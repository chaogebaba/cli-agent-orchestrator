"""F993 (#841): the ``cao_terminal`` host-capability gate must not swallow defects.

These live in their own module rather than in ``test_cao_server.py`` because
that file is ``pytestmark = pytest.mark.e2e`` and so is deselected by the
working ``-m "not e2e and not slow"`` selection. A guard against a false green
that itself only runs under ``--run-live`` would repeat the very mistake it
exists to prevent, so this module carries no marker and runs by default.

``provider_missing_from_host`` is the only thing standing between a failed
``POST /sessions`` and a ``pytest.skip``. Before F993 it skipped on any 5xx
whose body merely named the provider, which is essentially every
provider-related 500.
"""

from __future__ import annotations

from test.fixtures.cao_server import provider_missing_from_host

import pytest

# Bodies that mean the binary is genuinely absent from this host. These are the
# only shape allowed to downgrade a failure to a skip.
_ABSENT = (
    "Failed to create session: kiro-cli is not installed",
    "Failed to create session: kiro-cli: command not found",
    "[Errno 2] No such file or directory: 'kiro-cli'",
)

# Bodies that are real defects and must reach the test as a failure. The first
# is the F933 class -- a provider that starts but never reaches idle -- and is
# the specific regression this module exists to pin: it was previously skipped.
_DEFECTS = (
    "Failed to create session: kiro_cli initialization timed out after 15 seconds",
    "Failed to create session: mock_cli initialization timed out after 45 seconds",
    "Failed to create terminal: provider_launch_failed",
    "Internal Server Error",
    "kiro_cli",
    "Terminal metadata not found for terminal_id: ab8cfc5f",
    "session not found",
)


@pytest.mark.parametrize("body", _ABSENT)
def test_absent_binary_is_the_only_thing_that_skips(body: str) -> None:
    assert provider_missing_from_host(500, body) is True


@pytest.mark.parametrize("body", _DEFECTS)
def test_a_real_defect_never_skips(body: str) -> None:
    assert provider_missing_from_host(500, body) is False


def test_a_body_that_merely_names_the_provider_does_not_skip() -> None:
    """The catch-all that made every other marker decoration (F993).

    The removed entry was ``provider.lower()``, so any message naming the
    provider it failed to start matched -- which nearly all of them do.
    """
    for provider in ("kiro_cli", "claude_code", "codex", "mock_cli"):
        body = f"Failed to create session: {provider} blew up in a novel way"
        assert provider_missing_from_host(500, body) is False, provider


def test_initialization_timeout_is_not_a_host_fact() -> None:
    """Named separately because it was an explicit marker, not just collateral.

    A provider that starts but never reaches idle is F933: fuse_status held a
    quiescent terminal at PROCESSING for ~11s against a 15s budget. That was
    caught only because the test that hit it does not use this fixture.
    """
    assert provider_missing_from_host(500, "provider initialization timed out") is False


@pytest.mark.parametrize("status", [200, 201, 400, 401, 404, 409, 422, 499])
def test_non_5xx_never_skips_whatever_the_body_says(status: int) -> None:
    """A 4xx is a contract breach by the caller, never a host-capability fact."""
    assert provider_missing_from_host(status, "kiro-cli is not installed") is False


def test_the_marker_set_stayed_narrow() -> None:
    """Fails if someone widens the skip set without reading F993.

    Not a tautology over the module's own constant: the count and the exact
    strings are spelled out here, so adding a marker in cao_server.py turns
    this red and sends the author to the issue.
    """
    from test.fixtures.cao_server import _HOST_CANNOT_RUN_MARKERS

    assert set(_HOST_CANNOT_RUN_MARKERS) == {
        "not installed",
        "command not found",
        "no such file or directory",
    }, (
        "widening the host-capability skip set re-opens F993 (#841). A marker "
        "belongs here only if it can mean nothing except 'the binary is absent "
        "on this host' -- 'not found' alone is too broad (it matches 'session "
        "not found'), and 'initialization timed out' is a defect, not a host fact."
    )
