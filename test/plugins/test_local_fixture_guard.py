"""Canary test for the local_fixture marker guard (AC9).

A test marked with a nonexistent path must be xfailed at collection with
the decision-pointer reason.
"""

import pytest


@pytest.mark.local_fixture("/nonexistent/path/that/will/never/exist")
def test_canary_xfails_with_nonexistent_path():
    """This test body should never execute — xfail(run=False) skips it."""
    raise AssertionError("This should have been xfailed, not run")
