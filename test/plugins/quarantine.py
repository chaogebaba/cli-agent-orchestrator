"""Quarantine plugin (F254 D32).

Reads ``test/quarantine.toml`` at collection time and applies one of three
behaviours depending on the entry's ``class``:

  xdist_flaky  → xdist_group("quarantine-serial")
  worker_crash → xdist_group("quarantine-serial") + deselect under -n > 0
                 (unless CAO_TEST_QUARANTINE=run)
  known_red    → xfail(strict=False, run=True)

This hook runs AFTER tier_marks.py (trylast=True on the hook) so tier marks
are already applied.

Precedent: P-COLLECTHOOK (test/plugins/local_fixture_guard.py:17),
           P-LOADGROUP (--dist loadgroup honors xdist_group).

Registered via the ``pytest_plugins`` tuple in ``test/conftest.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.config import Config
    from _pytest.nodes import Item

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_QUARANTINE_FILE = Path(__file__).resolve().parent.parent / "quarantine.toml"
_VALID_CLASSES = frozenset({"xdist_flaky", "worker_crash", "known_red", "serial_only"})
_QUARANTINE_GROUP = "quarantine-serial"  # D34: named by resource/isolation


def _load_entries() -> list[dict]:
    """Parse quarantine.toml and return the entry list."""
    if not _QUARANTINE_FILE.exists():
        return []
    with open(_QUARANTINE_FILE, "rb") as f:
        data = tomllib.load(f)
    entries = data.get("entry", [])
    for e in entries:
        cls = e.get("class", "")
        if cls not in _VALID_CLASSES:
            raise ValueError(
                f"quarantine.toml: invalid class {cls!r} for {e.get('nodeid', '?')}"
            )
    return entries


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: Config, items: list[Item]) -> None:
    """Apply quarantine behaviours to collected items.

    tryfirst=True is REQUIRED: xdist/remote.py has its own
    pytest_collection_modifyitems (default priority) that reads xdist_group
    markers and appends @group suffixes to nodeids. If we run after xdist,
    it sees no markers and skips the suffix — loadgroup scheduler never
    groups the tests. Running first ensures our markers are visible to xdist.
    """
    entries = _load_entries()
    if not entries:
        return

    # D1 (F262): CAO_TEST_QUARANTINE=off makes the plugin fully inert —
    # no deselect, no xdist_group marker, no xfail. Restores exactly the
    # scheduling that filed the entry.
    if os.environ.get("CAO_TEST_QUARANTINE", "").lower() == "off":
        return

    # Build a lookup: nodeid → entry
    quarantine_map: dict[str, dict] = {e["nodeid"]: e for e in entries}

    # Determine parallelism level
    num_workers = _get_num_workers(config)
    force_run = os.environ.get("CAO_TEST_QUARANTINE", "").lower() == "run"

    deselected: list[Item] = []
    remaining: list[Item] = []

    for item in items:
        entry = quarantine_map.get(item.nodeid)
        if entry is None:
            remaining.append(item)
            continue

        cls = entry["class"]

        if cls == "xdist_flaky":
            # Serialize via the quarantine group (D34)
            item.add_marker(pytest.mark.xdist_group(_QUARANTINE_GROUP))
            remaining.append(item)

        elif cls == "serial_only":
            # D4 (F262): permanently serialized after diagnosis — same behaviour
            # as xdist_flaky but with no expiry and a mandatory verdict pointer.
            item.add_marker(pytest.mark.xdist_group(_QUARANTINE_GROUP))
            remaining.append(item)

        elif cls == "worker_crash":
            # Serialize + deselect under parallel unless forced
            if num_workers > 0 and not force_run:
                deselected.append(item)
            else:
                item.add_marker(pytest.mark.xdist_group(_QUARANTINE_GROUP))
                remaining.append(item)

        elif cls == "known_red":
            # xfail(strict=False, run=True) — recovery noticed as XPASS
            item.add_marker(
                pytest.mark.xfail(
                    strict=False,
                    run=True,
                    reason=f"quarantine: {entry.get('reason', 'known_red')}",
                )
            )
            remaining.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = remaining


def _get_num_workers(config: Config) -> int:
    """Return the xdist worker count (0 if serial).

    Under xdist, the controller has numprocesses=N but workers have
    numprocesses=None (xdist.remote line 397 sets it explicitly). We detect
    parallel mode by checking PYTEST_XDIST_WORKER_COUNT (set on workers) or
    the controller's numprocesses option.
    """
    # On an xdist worker, the env var is set
    worker_count = os.environ.get("PYTEST_XDIST_WORKER_COUNT")
    if worker_count is not None:
        try:
            return int(worker_count)
        except (ValueError, TypeError):
            pass

    # On the controller or non-xdist
    try:
        val = config.getoption("numprocesses", default=0)
        if val is None:
            return 0
        if isinstance(val, str) and val.lower() == "auto":
            return os.cpu_count() or 1
        return int(val)
    except (ValueError, TypeError):
        return 0
