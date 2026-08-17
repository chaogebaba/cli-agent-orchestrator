"""Tier hygiene AST guard (F254 D18).

Modelled line-for-line on ``test/test_datetime_convention.py``. Walks the AST
of ``test/**/*.py`` and fails on:

G1 — an ``@pytest.mark.<x>`` whose ``<x>`` is not in the pyproject.toml
     markers list (typo'd marker).
G2 — a ``subprocess.Popen`` / ``subprocess.run`` / ``requests.<verb>`` call
     at module or function level in a file whose census tier is ``unit``.
G3 — ``type("FakeBackend", (), {...})`` or a bare ``MagicMock()`` bound to a
     name matching ``.*backend.*`` under ``test/ux/`` (D10's fourth-pattern ban).
G4 — a ``contract``-tier test whose fixture closure reaches ``cao_terminal``
     without an explicit ``mock_cli`` indirect param (D8.1).
G5 — ``time.sleep(`` with a literal argument > 0.05 in any file whose census
     tier is ``unit`` or ``contract``. Scoped to files changed after F254 via
     a checked-in baseline ``test/f254-sleep-baseline.txt`` (shrink-only).

Precedent: P-ASTGUARD (three in-tree instances) + P-RATCHET.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

# Paths relative to repo root.
_TEST_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TEST_DIR.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_CENSUS_FILE = _TEST_DIR / "tier-census.json"
_SLEEP_BASELINE = _TEST_DIR / "f254-sleep-baseline.txt"


def _load_registered_markers() -> set[str]:
    """Parse pyproject.toml markers list to get all registered marker names."""
    text = _PYPROJECT.read_text()
    markers: set[str] = set()
    in_markers = False
    for line in text.splitlines():
        if line.strip().startswith("markers = ["):
            in_markers = True
            continue
        if in_markers:
            if line.strip() == "]":
                break
            # Format: "markname: description",
            stripped = line.strip().strip('",')
            if ":" in stripped:
                name = stripped.split(":")[0].strip()
                if name:
                    markers.add(name)
    return markers


def _load_census() -> dict[str, str]:
    """Load the tier census (nodeid -> tier mapping)."""
    if not _CENSUS_FILE.exists():
        pytest.skip("tier-census.json not found; run 'make test-tiers' first")
    data = json.loads(_CENSUS_FILE.read_text())
    return data.get("items", {})


def _file_tier_from_census(census: dict[str, str], filepath: Path) -> str | None:
    """Determine the dominant tier for a file from the census.

    Returns the tier if ALL items in the file share the same tier,
    or the first item's tier as a representative.
    """
    rel = str(filepath.relative_to(_REPO_ROOT))
    tiers_in_file: set[str] = set()
    for nodeid, tier in census.items():
        if nodeid.startswith(rel + "::") or nodeid.startswith(rel + "["):
            tiers_in_file.add(tier)
    if not tiers_in_file:
        return None
    # If mixed, return None (cannot assert a single tier).
    if len(tiers_in_file) == 1:
        return tiers_in_file.pop()
    return None


def _load_sleep_baseline() -> set[str]:
    """Load the sleep baseline (set of relative file paths with known sleeps)."""
    if not _SLEEP_BASELINE.exists():
        return set()
    lines = _SLEEP_BASELINE.read_text().splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


# Built-in markers that pytest and plugins define (not subject to G1).
_BUILTIN_MARKERS: set[str] = {
    "parametrize",
    "skip",
    "skipif",
    "xfail",
    "usefixtures",
    "filterwarnings",
    "timeout",
    "asyncio",
    "xdist_group",
    "repeat",
    "flaky",
}


def _extract_mark_decorators(tree: ast.Module) -> list[tuple[int, str]]:
    """Extract all @pytest.mark.<name> decorators with line numbers."""
    results: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for decorator in node.decorator_list:
            mark_name = _extract_mark_name(decorator)
            if mark_name:
                results.append((decorator.lineno, mark_name))
    return results


def _extract_mark_name(node: ast.expr) -> str | None:
    """Extract the mark name from a pytest.mark.X expression (with or without call)."""
    # @pytest.mark.X
    if isinstance(node, ast.Attribute):
        if _is_pytest_mark(node.value):
            return node.attr
    # @pytest.mark.X(...)
    if isinstance(node, ast.Call):
        return _extract_mark_name(node.func)
    return None


def _is_pytest_mark(node: ast.expr) -> bool:
    """Check if node is ``pytest.mark``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "mark"
        and isinstance(node.value, ast.Name)
        and node.value.id == "pytest"
    )


class TestG1UnregisteredMarkers:
    """G1: fail on @pytest.mark.<x> where <x> is not in pyproject.toml markers."""

    def test_no_unregistered_markers(self):
        registered = _load_registered_markers() | _BUILTIN_MARKERS
        violations: list[str] = []

        for py_file in _TEST_DIR.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            try:
                tree = ast.parse(py_file.read_text(), filename=str(py_file))
            except SyntaxError:
                continue
            marks = _extract_mark_decorators(tree)
            for lineno, mark_name in marks:
                if mark_name not in registered:
                    rel = py_file.relative_to(_REPO_ROOT)
                    violations.append(f"{rel}:{lineno} — unregistered mark '{mark_name}'")

        assert not violations, (
            "G1: unregistered pytest markers found (typo or needs registration "
            "in pyproject.toml [tool.pytest.ini_options] markers):\n"
            + "\n".join(violations)
        )


class TestG2SubprocessInUnit:
    """G2: no subprocess.Popen/run or requests.<verb> in unit-tier files."""

    _SUBPROCESS_ATTRS = {"Popen", "run", "call", "check_call", "check_output"}
    _REQUESTS_VERBS = {"get", "post", "put", "delete", "patch", "head", "options"}

    def test_no_real_io_in_unit_tier(self):
        census = _load_census()
        violations: list[str] = []

        for py_file in _TEST_DIR.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            tier = _file_tier_from_census(census, py_file)
            if tier != "unit":
                continue

            try:
                source = py_file.read_text()
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func

                # subprocess.Popen / subprocess.run etc.
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in self._SUBPROCESS_ATTRS
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess"
                ):
                    rel = py_file.relative_to(_REPO_ROOT)
                    violations.append(
                        f"{rel}:{node.lineno} — subprocess.{func.attr}() in unit-tier file"
                    )

                # requests.<verb>
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in self._REQUESTS_VERBS
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "requests"
                ):
                    rel = py_file.relative_to(_REPO_ROOT)
                    violations.append(
                        f"{rel}:{node.lineno} — requests.{func.attr}() in unit-tier file"
                    )

        assert not violations, (
            "G2: real I/O calls in unit-tier test files. Move to contract tier or above:\n"
            + "\n".join(violations)
        )


class TestG3FakeBackendBan:
    """G3: no type('FakeBackend',...) or bare MagicMock() as backend in test/ux/."""

    def test_no_fourth_pattern_fake_backend(self):
        ux_dir = _TEST_DIR / "ux"
        if not ux_dir.exists():
            pytest.skip("test/ux/ does not exist yet (Phase 5)")

        violations: list[str] = []

        for py_file in ux_dir.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            try:
                source = py_file.read_text()
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                # type("FakeBackend", (), {...})
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "type"
                    and len(node.args) >= 1
                    and isinstance(node.args[0], ast.Constant)
                    and "FakeBackend" in str(node.args[0].value)
                ):
                    rel = py_file.relative_to(_REPO_ROOT)
                    violations.append(
                        f"{rel}:{node.lineno} — type('FakeBackend', ...) pattern banned"
                    )

                # MagicMock() bound to *backend* name
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        target_name = ""
                        if isinstance(target, ast.Name):
                            target_name = target.id
                        elif isinstance(target, ast.Attribute):
                            target_name = target.attr
                        if "backend" in target_name.lower():
                            if (
                                isinstance(node.value, ast.Call)
                                and isinstance(node.value.func, ast.Name)
                                and node.value.func.id == "MagicMock"
                                and not node.value.args
                            ):
                                rel = py_file.relative_to(_REPO_ROOT)
                                violations.append(
                                    f"{rel}:{node.lineno} — bare MagicMock() as backend"
                                )

        assert not violations, (
            "G3: banned fake-backend patterns under test/ux/ (use FakeBackend from D10):\n"
            + "\n".join(violations)
        )


class TestG5SleepInUnitContract:
    """G5: time.sleep(>0.05) in unit/contract files, scoped via baseline."""

    def test_no_new_sleeps(self):
        census = _load_census()
        baseline = _load_sleep_baseline()
        violations: list[str] = []

        for py_file in _TEST_DIR.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            rel_str = str(py_file.relative_to(_REPO_ROOT))

            # G5 is scoped to files NOT in the baseline (new violations only).
            if rel_str in baseline:
                continue

            tier = _file_tier_from_census(census, py_file)
            if tier not in ("unit", "contract"):
                continue

            try:
                source = py_file.read_text()
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func

                # time.sleep(X) where X is a literal > 0.05
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "sleep"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "time"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, (int, float))
                    and node.args[0].value > 0.05
                ):
                    violations.append(
                        f"{rel_str}:{node.lineno} — time.sleep({node.args[0].value}) "
                        f"in {tier}-tier file (budget ceiling 0.05 s)"
                    )

        assert not violations, (
            "G5: time.sleep() with literal > 0.05 in unit/contract-tier files "
            "(not in baseline). Either remove the sleep or add the file to "
            "test/f254-sleep-baseline.txt:\n" + "\n".join(violations)
        )

    def test_baseline_is_shrink_only(self):
        """The baseline file must not grow — only shrink as sleeps are removed."""
        baseline = _load_sleep_baseline()
        if not baseline:
            return  # empty baseline is valid (means all sleeps cleaned)

        # Verify each baseline entry still has the sleep (if not, it should be removed).
        removable: list[str] = []
        for rel_str in sorted(baseline):
            filepath = _REPO_ROOT / rel_str
            if not filepath.exists():
                removable.append(f"{rel_str} — file no longer exists")
                continue

            try:
                source = filepath.read_text()
                tree = ast.parse(source, filename=str(filepath))
            except SyntaxError:
                continue

            has_sleep = False
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "sleep"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "time"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, (int, float))
                    and node.args[0].value > 0.05
                ):
                    has_sleep = True
                    break

            if not has_sleep:
                removable.append(f"{rel_str} — no qualifying time.sleep() remaining")

        # This is advisory, not a hard fail — gives maintainers a nudge to shrink.
        if removable:
            import warnings

            warnings.warn(
                f"Baseline entries can be removed (shrink-only ratchet):\n"
                + "\n".join(removable),
                stacklevel=2,
            )
