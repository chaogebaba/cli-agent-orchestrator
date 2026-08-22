"""Enforce aware-UTC datetime convention fork-wide."""
import ast
from pathlib import Path

import pytest

# F254 D19: entire module exceeds unit budget (AST walk over full source tree).
pytestmark = pytest.mark.slow

SRC = Path(__file__).resolve().parents[1] / "src" / "cli_agent_orchestrator"


def _collect_datetime_aliases(tree: ast.Module) -> set[str]:
    """Collect local names that alias the datetime class."""
    aliases = {"datetime"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            for alias in node.names:
                if alias.name == "datetime":
                    aliases.add(alias.asname or alias.name)
    return aliases


def test_no_naive_datetime_calls():
    """No bare datetime.now() or datetime.utcnow() in production source."""
    violations = []
    for py in SRC.rglob("*.py"):
        source = py.read_text()
        tree = ast.parse(source, filename=str(py))
        aliases = _collect_datetime_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("now", "utcnow"):
                name_node = func.value
                # Simple: datetime.now()
                if isinstance(name_node, ast.Name) and name_node.id in aliases:
                    if func.attr == "now" and node.args:
                        continue  # has tz arg = _utcnow or equivalent
                    violations.append(f"{py.relative_to(SRC)}:{node.lineno}")
                # Module-qualified: datetime.datetime.now()
                elif (
                    isinstance(name_node, ast.Attribute)
                    and name_node.attr == "datetime"
                    and isinstance(name_node.value, ast.Name)
                    and name_node.value.id == "datetime"
                ):
                    if func.attr == "now" and node.args:
                        continue
                    violations.append(f"{py.relative_to(SRC)}:{node.lineno}")
    assert not violations, "Naive datetime calls found:\n" + "\n".join(violations)


def test_no_utcnow_grep():
    """Belt-and-suspenders: raw grep for deprecated utcnow()."""
    violations = []
    for py in SRC.rglob("*.py"):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if "utcnow()" in line and "def _utcnow" not in line and "_utcnow()" not in line and "clock.utcnow()" not in line:  # _utcnow() exclusion: EMPIRICAL S1 fold (43 FPs on the sanctioned helper); clock.utcnow(): D3 sim seam
                if line.lstrip().startswith("#"):
                    continue
                violations.append(f"{py.relative_to(SRC)}:{i}")
    assert not violations, "utcnow() calls found:\n" + "\n".join(violations)
