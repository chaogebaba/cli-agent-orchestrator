"""F1004 (#852) — the skill CLI names ``public_api`` and nothing else in the base package.

``pyproject.toml``'s ``skill-cli-only-via-public-api`` contract already forbids the seam
from importing any base module but ``cli_agent_orchestrator.public_api``. Import-linter
matches modules, not names, so one thing is left over: a private *name* taken out of a
module the seam is allowed to import (``from ...public_api.markdown_fold import
_read_markdown`` would satisfy every contract and re-open exactly the hazard F1004 closes).
This file reads the seam's own source and asserts both halves at name granularity.

It parses source rather than importing it, so a module added under
``cli/orchestrator_commands/`` is covered the day it lands, with no registration step.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator"
SEAM_DIR = SRC / "cli" / "orchestrator_commands"
PUBLIC = "cli_agent_orchestrator.public_api"
BASE = "cli_agent_orchestrator"


def _seam_modules() -> list[Path]:
    modules = sorted(path for path in SEAM_DIR.glob("*.py"))
    modules.append(SRC / "cli" / "orchestrator_main.py")
    assert len(modules) >= 4, f"seam looks empty: {modules}"
    return modules


def _base_imports(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    """Every ``cli_agent_orchestrator`` import in ``path`` as (module, imported names).

    ``import cli_agent_orchestrator`` — the bare root package, whose only use is its
    ``__file__`` — is not collected. It binds no submodule and takes no name out of one,
    so it is not a seam crossing; the published way to ask is
    ``public_api.workspace.package_root()``. ``from cli_agent_orchestrator import X`` is
    a different statement and IS collected.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import; inside this package it still resolves into
            # the base package, so it must not be a way around the check.
            module = node.module or ""
            if node.level:
                parts = path.relative_to(SRC.parent).with_suffix("").parts[: -node.level]
                module = ".".join((*parts, module)) if module else ".".join(parts)
            if module == BASE or module.startswith(f"{BASE}."):
                found.append((module, tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(f"{BASE}."):
                    found.append((alias.name, ()))
    return found


@pytest.mark.parametrize("path", _seam_modules(), ids=lambda path: path.name)
def test_seam_module_imports_only_the_public_api(path: Path) -> None:
    """The module half — asserted here too, so a contract file someone edits is not the
    only witness that the seam holds."""
    for module, _names in _base_imports(path):
        if path.name == "orchestrator_main.py" and module.startswith(
            f"{BASE}.cli.orchestrator_commands"
        ):
            continue  # the entry point importing its own commands IS the structure
        assert module == PUBLIC or module.startswith(
            f"{PUBLIC}."
        ), f"{path.name} reaches into the base package at {module!r}; import it from {PUBLIC}"


@pytest.mark.parametrize("path", _seam_modules(), ids=lambda path: path.name)
def test_seam_module_imports_no_private_name(path: Path) -> None:
    """The name half, which no import-linter contract can see."""
    for module, names in _base_imports(path):
        private = tuple(name for name in names if name.startswith("_"))
        assert not private, (
            f"{path.name} imports private name(s) {private} from {module!r}; "
            "publish what it needs in public_api instead"
        )
