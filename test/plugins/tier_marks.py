"""Tier derivation plugin (F254 D15/D17).

Assigns exactly one tier mark to every collected item by the first matching
rule, evaluated in order:

  1. item declares ``live``, ``e2e``, ``pty``, or ``slow`` explicitly
  2. path contains ``/test/e2e/`` and not ``/test/e2e/script_runner/``
  3. path contains ``/test/simulation/``
  4. static fixture closure intersects the contract-fixture set
  5. item declares ``integration`` explicitly
  6. otherwise → ``unit``

Rule 4 is parameter-blind: ``item.fixturenames`` at collection time reports
the full transitive closure regardless of parametrize indirection.

The plugin also exposes ``--tier-report=<path>`` which dumps the tier census
as JSON after collection (D17).  Combined with ``--collect-only -n 0``, this
produces the census cheaply.

Registered via the ``pytest_plugins`` tuple in ``test/conftest.py``.

Precedent: P-COLLECTHOOK — test/e2e/conftest.py:29, test/plugins/smoke_tags.py:52,
test/plugins/local_fixture_guard.py:17.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.config import Config
    from _pytest.config.argparsing import Parser

# Tier marks that, when declared explicitly, take priority (rule 1).
_EXPLICIT_TIER_MARKS: frozenset[str] = frozenset({"live", "e2e", "pty", "slow"})

# Fixtures whose presence in the static closure implies contract tier (rule 4).
_CONTRACT_FIXTURES: frozenset[str] = frozenset(
    {
        "cao_server",
        "cao_server_with_auth",
        "cao_terminal",
        "cao_terminal_mock",
        "cao_terminal_authed",
    }
)

# All recognized tier names (the complete vocabulary).
_ALL_TIERS: frozenset[str] = frozenset(
    {"unit", "contract", "integration", "sim", "slow", "live", "e2e", "pty"}
)


def pytest_addoption(parser: "Parser") -> None:
    """Register the --tier-report CLI option (D17)."""
    parser.addoption(
        "--tier-report",
        default=None,
        metavar="PATH",
        help="Write tier census JSON (nodeid->tier mapping) to PATH after collection.",
    )


def pytest_collection_modifyitems(config: "Config", items: list[pytest.Item]) -> None:
    """Derive and assign tier marks to all collected items.

    This hook MUST run before enforcement plugins (Phase 3) and after
    smoke_tags.py (which only adds ``smoke`` — not a tier mark here).

    The hook also subsumes test/e2e/conftest.py's previous modifyitems logic
    (rule 2 adds ``live`` to /test/e2e/ items excluding script_runner/).
    """
    census: dict[str, str] = {}

    for item in items:
        tier = _derive_tier(item)
        # Add the derived tier as a real marker so it is visible to -m filters.
        item.add_marker(getattr(pytest.mark, tier))
        census[item.nodeid] = tier

    # Stash the census on config for other plugins / the matrix test (D6).
    config._tier_census = census  # type: ignore[attr-defined]

    # Write the tier report if requested (D17).
    report_path = config.getoption("--tier-report", default=None)
    if report_path:
        _write_tier_report(report_path, census)


def _derive_tier(item: pytest.Item) -> str:
    """Return the single tier name for *item*, per D15 rule table."""
    # Collect the item's own marker names for rule 1 and 5.
    own_markers: set[str] = set()
    for mark in item.iter_markers():
        own_markers.add(mark.name)

    # Rule 1: explicit tier mark takes priority.
    for mark_name in _EXPLICIT_TIER_MARKS:
        if mark_name in own_markers:
            return mark_name

    # Rule 2: path-based e2e detection (subsumes test/e2e/conftest.py:29).
    path_str = str(item.path)
    if "/test/e2e/" in path_str and "/test/e2e/script_runner/" not in path_str:
        return "live"

    # Rule 3: simulation directory.
    if "/test/simulation/" in path_str:
        return "sim"

    # Rule 4: fixture closure intersects the contract fixture set.
    # item.fixturenames is the static transitive closure, available at
    # collection time (before parametrize resolution — parameter-blind).
    if hasattr(item, "fixturenames") and _CONTRACT_FIXTURES & set(item.fixturenames):
        return "contract"

    # Rule 5: explicit integration mark.
    if "integration" in own_markers:
        return "integration"

    # Rule 6: fallback.
    return "unit"


def _write_tier_report(path: str, census: dict[str, str]) -> None:
    """Write the census JSON: per-tier counts + full membership list."""
    # Compute per-tier counts.
    counts: dict[str, int] = {}
    for tier in sorted(_ALL_TIERS):
        counts[tier] = 0
    for tier in census.values():
        counts[tier] = counts.get(tier, 0) + 1

    report = {
        "total": len(census),
        "counts": counts,
        "items": census,
    }

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n")
