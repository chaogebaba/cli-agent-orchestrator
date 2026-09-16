"""A verdict run may not be satisfied by skipped real-browser arms (B1 fix 2).

The real-browser oracle is load-bearing for AC-25/AC-35, and it is ``e2e`` +
``slow``. Before this guard, a closure round on a box without Chromium printed
``17 skipped`` and exited 0 — indistinguishable from a pass to anything reading
the return code. ``CAO_F862_REQUIRE_BROWSER=1`` is what a round that is being
read as a verdict sets; with it, an unreachable browser FAILS.

These are offline unit tests of the gate itself, not of the browser.
"""

from __future__ import annotations

from test.fixtures import chatgpt_web_real_browser as harness

import pytest


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_truthy_spellings_demand_a_verdict(monkeypatch, value):
    monkeypatch.setenv(harness.REQUIRE_BROWSER_ENV, value)
    assert harness.browser_required() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_everything_else_leaves_the_run_exploratory(monkeypatch, value):
    monkeypatch.setenv(harness.REQUIRE_BROWSER_ENV, value)
    assert harness.browser_required() is False


def test_unset_leaves_the_run_exploratory(monkeypatch):
    monkeypatch.delenv(harness.REQUIRE_BROWSER_ENV, raising=False)
    assert harness.browser_required() is False


def test_absent_browser_skips_an_exploratory_run(monkeypatch):
    """Without the flag the honest skip is still the behaviour."""
    monkeypatch.delenv(harness.REQUIRE_BROWSER_ENV, raising=False)
    monkeypatch.setattr(harness, "chromium_available", lambda: False)
    assert harness.browser_skip_condition() is True
    harness.enforce_browser_requirement()  # must not raise


def test_absent_browser_FAILS_a_verdict_run(monkeypatch):
    """The closure property: rc must not be satisfiable by skips."""
    monkeypatch.setenv(harness.REQUIRE_BROWSER_ENV, "1")
    monkeypatch.setattr(harness, "chromium_available", lambda: False)
    monkeypatch.setattr(harness, "chromium_unavailable_reason", lambda: "no chromium here")

    # The arms are NOT skipped, so the autouse guard gets to run and fail them.
    assert harness.browser_skip_condition() is False
    # pytest.fail raises BaseException, not Exception: a bare `except Exception`
    # in a runner must NOT be able to swallow the verdict guard.
    with pytest.raises(pytest.fail.Exception) as excinfo:
        harness.enforce_browser_requirement()
    message = str(excinfo.value)
    assert harness.REQUIRE_BROWSER_ENV in message
    assert "no chromium here" in message
    assert "playwright install chromium" in message


def test_present_browser_runs_normally_under_the_flag(monkeypatch):
    monkeypatch.setenv(harness.REQUIRE_BROWSER_ENV, "1")
    monkeypatch.setattr(harness, "chromium_available", lambda: True)
    assert harness.browser_skip_condition() is False
    harness.enforce_browser_requirement()  # must not raise
