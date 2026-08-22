"""local_fixture marker guard (fx147, D4).

Tests marked ``@pytest.mark.local_fixture("/path/to/dir")`` are xfailed at
collection when the given path does not exist, with a reason pointing back to
the F149 decision. This is the single absence guard — no separate skipif, no
AST scanning.

Registered via the ``pytest_plugins`` tuple in ``test/conftest.py`` (project
idiom — same as ``rss_guard.py``).
"""

from pathlib import Path

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        marker = item.get_closest_marker("local_fixture")
        if marker is None:
            continue
        if not marker.args:
            continue
        fixture_path = Path(marker.args[0])
        if not fixture_path.exists():
            item.add_marker(
                pytest.mark.xfail(
                    reason=(
                        f"local_fixture path does not exist: {fixture_path} "
                        f"(fx147 D4: fixture files not available; F149 stays open)"
                    ),
                    run=False,
                )
            )
