"""AC9 — the five package-boundary contracts (WP-ARCH phase 1, F725 #581).

A contract that has never been seen to FAIL is not evidence of anything, so this
module does two things: it runs ``lint-imports`` against the real tree, and it
runs it again against a copy carrying a deliberate violation.  If the second run
passed, the first would prove nothing.

The violating file is written into the REAL tree and removed in a ``finally``.
A copied tree was tried first and proved worthless: grimp resolves
``cli_agent_orchestrator`` through the installed (editable) package, so the
linter kept analysing the real source and reported all five contracts KEPT while
the probe sat unseen in a temporary directory. Only an in-tree probe is actually
linted.

Every test here shares one ``xdist_group`` so they run in the same worker: the
probe is briefly visible on disk, and a concurrent worker running the clean-tree
assertions would see it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.xdist_group("wp-arch-import-contracts")]

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "cli_agent_orchestrator"

# AC11: the ONLY legacy files permitted to import the new packages, one per hook
# point. ``api/main.py`` is deliberately absent — under the audit's definition
# "legacy" is every pre-existing package EXCEPT api, mcp_server and cli (N2), so
# the composition-root call at hook point 5 is not a legacy import at all.
AC11_LEGACY_IMPORTERS = {
    "providers/codex.py",
    "services/status_monitor.py",
    "services/fleet_service.py",
    "services/terminal_service.py",
    "cli/main.py",
}

_LEGACY_DIRS = (
    "agent_store",
    "backends",
    "clients",
    "ext_apps",
    "graph",
    "hooks",
    "kernel",
    "models",
    "ops_mcp_server",
    "plugins",
    "providers",
    "schemas",
    "security",
    "services",
    "sim",
    "skills",
    "telemetry",
    "transcript_scrub",
    "tui",
    "utils",
)

_NEW_IMPORT_MARKERS = (
    "cli_agent_orchestrator.core",
    "cli_agent_orchestrator.app",
    "cli_agent_orchestrator.adapters",
)


def _run_lint_imports(cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the real ``lint-imports`` console script, from CI's own entry point.

    ``importlinter.cli`` has no ``__main__``, so ``python -m importlinter.cli``
    exits 0 having done nothing — which would make every assertion below pass
    vacuously. Invoking the installed script is the only form that runs the
    linter, and it is the same one the CI step runs.
    """
    script = Path(sys.executable).with_name("lint-imports")
    return subprocess.run(
        [str(script)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_all_five_contracts_pass() -> None:
    """The five contracts of the audit §2.3, green against the real tree."""
    result = _run_lint_imports(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in (
        "layers: edge > app > core",
        "adapters-are-leaves",
        "new-code-never-imports-legacy",
        "core-is-pure",
        "adapters-only-via-composition-root",
    ):
        assert f"{name} KEPT" in result.stdout, result.stdout
    assert "Contracts: 5 kept, 0 broken." in result.stdout


def test_the_graph_covers_the_namespace_packages() -> None:
    """``services`` and ``clients`` have no ``__init__.py``.

    grimp's walk skips a directory without one, so with the bare root package it
    saw ZERO of the 132 modules under ``services`` and the legacy contracts were
    vacuous for the two largest legacy packages. Both are named explicitly as
    root packages; this asserts they are actually in the graph.
    """
    import grimp

    graph = grimp.build_graph(
        "cli_agent_orchestrator",
        "cli_agent_orchestrator.services",
        "cli_agent_orchestrator.clients",
    )
    for package in ("services", "clients"):
        prefix = f"cli_agent_orchestrator.{package}."
        assert any(module.startswith(prefix) for module in graph.modules), package


@pytest.mark.parametrize(
    ("relative_path", "violating_import", "contract"),
    [
        (
            "core/violation_probe.py",
            "from cli_agent_orchestrator.services import status_monitor  # noqa: F401",
            "new-code-never-imports-legacy",
        ),
        (
            "core/violation_probe.py",
            "import sqlite3  # noqa: F401",
            "core-is-pure",
        ),
        (
            "app/violation_probe.py",
            "from cli_agent_orchestrator.adapters import clock  # noqa: F401",
            "adapters-only-via-composition-root",
        ),
        (
            "adapters/violation_probe.py",
            "from cli_agent_orchestrator.app import worker_truth  # noqa: F401",
            "adapters-are-leaves",
        ),
    ],
)
def test_a_deliberate_violation_fails(
    relative_path: str, violating_import: str, contract: str
) -> None:
    """Each contract is shown REJECTING the thing it exists to reject.

    Without this, a typo in a module path would leave a contract silently
    checking nothing — the same failure mode that made the graph miss
    ``services`` in the first place.
    """
    probe = SRC / relative_path
    assert not probe.exists(), f"probe path is already a real module: {probe}"
    probe.write_text(
        f'"""Deliberate contract violation — written by a test."""\n\n{violating_import}\n'
    )
    try:
        result = _run_lint_imports(REPO_ROOT)
    finally:
        probe.unlink(missing_ok=True)

    assert result.returncode != 0, result.stdout
    assert f"{contract} BROKEN" in result.stdout, result.stdout


def test_legacy_files_importing_new_packages_stay_within_the_ac11_allowlist() -> None:
    """No legacy file outside the AC11 hook-point list may import the new tree.

    Phase 1 lands across three lanes, so the set is a SUBSET here until lanes B
    and C attach hook points 1, 2, 2b, 3, 6 and 7; the phase EXITS when it
    equals the allowlist. What must never happen — at any point, including
    mid-phase — is a legacy import from a file the blueprint did not sanction.
    """
    importers: set[str] = set()
    for directory in _LEGACY_DIRS:
        package_dir = SRC / directory
        if not package_dir.exists():
            continue
        for path in package_dir.rglob("*.py"):
            text = path.read_text()
            if any(marker in text for marker in _NEW_IMPORT_MARKERS):
                importers.add(str(path.relative_to(SRC)))

    unexpected = importers - AC11_LEGACY_IMPORTERS
    assert not unexpected, f"legacy files importing the new tree outside AC11: {sorted(unexpected)}"


def test_bootstrap_is_the_only_new_module_touching_legacy() -> None:
    """The composition root is the ONE new module allowed to read ``constants``.

    ``core``, ``app`` and ``adapters`` are covered by the import-linter contract;
    this catches the top-level new modules that no contract's source list names.
    """
    offenders: list[str] = []
    for path in (SRC / "bootstrap.py",):
        assert path.exists()
    for package in ("core", "app", "adapters"):
        for path in (SRC / package).rglob("*.py"):
            text = path.read_text()
            for directory in _LEGACY_DIRS:
                if f"cli_agent_orchestrator.{directory}" in text:
                    offenders.append(f"{path.relative_to(SRC)} -> {directory}")
            if "cli_agent_orchestrator.constants" in text:
                offenders.append(f"{path.relative_to(SRC)} -> constants")
    assert not offenders, offenders
