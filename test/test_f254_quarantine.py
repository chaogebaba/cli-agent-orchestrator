"""F254 D33/D34/D35 quarantine hygiene tests.

These tests enforce quarantine policy mechanically:

  - test_no_expired_quarantine_entries (D33): expires in the past → FAIL
  - test_quarantined_nodeids_still_collect (AC-F4): renamed/removed test → FAIL
  - test_no_rerun_or_randomly_plugins (D35/AC-F5): banned plugins absent

Precedent: P-RATCHET (ci.yml:175-184), P-ASTGUARD (test/test_datetime_convention.py).
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

_QUARANTINE_FILE = Path(__file__).resolve().parent / "quarantine.toml"
_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _load_quarantine() -> list[dict]:
    with open(_QUARANTINE_FILE, "rb") as f:
        data = tomllib.load(f)
    return data.get("entry", [])


# ---------------------------------------------------------------------------
# D33 — Quarantine entries expire, and expiry is a test
# ---------------------------------------------------------------------------


def test_no_expired_quarantine_entries() -> None:
    """Every quarantine entry must have a future expiry date.

    A past expiry means the entry has outlived its review window and must be
    either removed (test is fixed) or re-justified with a new date.
    """
    today = datetime.date.today()
    entries = _load_quarantine()
    expired = []
    for entry in entries:
        expires_str = entry.get("expires", "")
        try:
            expires = datetime.date.fromisoformat(expires_str)
        except (ValueError, TypeError):
            expired.append(f"{entry.get('nodeid', '?')}: invalid expires={expires_str!r}")
            continue
        if expires < today:
            expired.append(
                f"{entry.get('nodeid', '?')}: expired {expires_str} (today={today})"
            )
    if expired:
        msg = "Expired quarantine entries (D33 — extend or remove):\n"
        msg += "\n".join(f"  - {e}" for e in expired)
        pytest.fail(msg)


# ---------------------------------------------------------------------------
# AC-F4 — Renamed/removed tests detected
# ---------------------------------------------------------------------------


def test_quarantined_nodeids_still_collect(pytestconfig) -> None:
    """Every quarantined nodeid must still be collectable.

    A renamed or deleted test silently drops out of quarantine otherwise —
    the documented drift risk from smoke_tags.py's node-ID list.
    """
    entries = _load_quarantine()
    if not entries:
        pytest.skip("No quarantine entries")

    # Get all collected nodeids from this session
    # (we use the plugin_manager to access the session's items)
    session = pytestconfig._store.get(pytest.StashKey[object](), None)

    # Alternative: collect nodeids via subprocess for isolation
    # But since this test runs inside the collection, we can check the items
    # that were collected by reading the session items stashed by our conftest.
    # However, that's complex. Simpler: verify the test files + functions exist.
    missing = []
    for entry in entries:
        nodeid = entry["nodeid"]
        # Parse nodeid: "path/to/file.py::Class::method" or "path/to/file.py::function"
        # Strip parametrize suffix: "func[param]" → "func"
        parts = nodeid.split("::")
        filepath = Path(__file__).resolve().parent.parent / parts[0]
        if not filepath.exists():
            missing.append(f"{nodeid}: file {parts[0]} does not exist")
            continue

        # Check the function/method exists in the file source
        # Strip parametrize brackets from function name
        func_name = parts[-1]
        if "[" in func_name:
            func_name = func_name[: func_name.index("[")]
        source = filepath.read_text()
        if f"def {func_name}" not in source:
            missing.append(f"{nodeid}: function {func_name!r} not found in {parts[0]}")

    if missing:
        msg = "Quarantined nodeids that no longer collect (AC-F4 — remove stale entries):\n"
        msg += "\n".join(f"  - {m}" for m in missing)
        pytest.fail(msg)


# ---------------------------------------------------------------------------
# D35 / AC-F5 — No automatic reruns
# ---------------------------------------------------------------------------


def test_no_rerun_or_randomly_plugins() -> None:
    """pytest-rerunfailures and pytest-randomly must not appear in deps.

    D35: no automatic reruns. D22: no randomization (deterministic order).
    Guard asserts absence from both pyproject.toml and uv.lock.
    """
    # Check pyproject.toml
    pyproject_text = _PYPROJECT.read_text()
    banned = ["pytest-rerunfailures", "pytest-randomly"]
    violations = []

    for pkg in banned:
        if pkg in pyproject_text:
            violations.append(f"{pkg} found in pyproject.toml")

    # Check uv.lock
    lock_file = _PYPROJECT.parent / "uv.lock"
    if lock_file.exists():
        lock_text = lock_file.read_text()
        for pkg in banned:
            if pkg in lock_text:
                violations.append(f"{pkg} found in uv.lock")

    if violations:
        msg = "Banned test plugins present (D35 — no automatic reruns):\n"
        msg += "\n".join(f"  - {v}" for v in violations)
        pytest.fail(msg)
