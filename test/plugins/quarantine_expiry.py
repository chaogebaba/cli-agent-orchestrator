"""Quarantine expiry enforcement (WP-SUITE D5, AC5.3).

Checks ``test/quarantine.toml`` at collection time: if any entry's ``review_by``
date is in the past, pytest exits with a loud error. This makes ``make test-hygiene``
the enforcement surface — the ledger's own expiry is checked by the suite, not
by memory.

Activation: only fires when ``CAO_TEST_TIER_BUDGET=enforce`` is set (the same
env var that ``test-hygiene`` uses), so normal ``test-quick`` / ``test-ci`` runs
are not blocked by an expired entry. The hygiene target is the single place that
enforces burn-down discipline.

Registered via ``pytest_plugins`` in ``test/conftest.py``.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.config import Config

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

_QUARANTINE_FILE = Path(__file__).resolve().parent.parent / "quarantine.toml"


def pytest_configure(config: Config) -> None:
    """Fail early if any quarantine entry has expired (review_by in the past).

    Only active under CAO_TEST_TIER_BUDGET=enforce (the test-hygiene env).
    """
    if os.environ.get("CAO_TEST_TIER_BUDGET", "").lower() != "enforce":
        return

    if not _QUARANTINE_FILE.exists():
        return

    with open(_QUARANTINE_FILE, "rb") as f:
        data = tomllib.load(f)

    entries = data.get("entry", [])
    if not entries:
        return

    today = date.today()
    expired: list[str] = []

    for entry in entries:
        nodeid = entry.get("nodeid", "<unknown>")

        # Validate filed field exists
        filed_str = entry.get("filed")
        if not filed_str:
            expired.append(f"  {nodeid}: missing filed field")
            continue

        review_by_str = entry.get("review_by")
        if not review_by_str:
            # Missing review_by is itself a violation
            expired.append(f"  {nodeid}: missing review_by field")
            continue

        try:
            review_date = date.fromisoformat(review_by_str)
        except (ValueError, TypeError):
            expired.append(f"  {nodeid}: invalid review_by={review_by_str!r}")
            continue

        if review_date < today:
            expired.append(
                f"  {nodeid}: review_by={review_by_str} (expired {(today - review_date).days}d ago)"
            )

    if expired:
        msg = (
            "\n\nQUARANTINE EXPIRY VIOLATION (D5 AC5.3)\n"
            "The following quarantine entries have passed their review_by date.\n"
            "Each must be resolved: fix the test (remove entry), re-file with a\n"
            "new review_by (document progress in reason), or escalate.\n\n"
            + "\n".join(expired)
            + "\n"
        )
        pytest.exit(msg, returncode=1)
