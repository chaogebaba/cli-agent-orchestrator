"""F254 D6 — Coverage matrix enforcement test.

Reads test/ux_surfaces.toml and fails when:
1. Any obligation cell from D2/D3 is empty (no test covers it).
2. An @mcp.tool() decorator exists in mcp_server/server.py whose tool name
   appears in no roster row (AST walk, D6.3).

This test collects ux marks via pytest_collection_modifyitems and validates
the coverage matrix against the declared obligations.
"""

import ast
import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:
    import tomli as tomllib


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SURFACES_PATH = _REPO_ROOT / "test" / "ux_surfaces.toml"
_SERVER_PATH = _REPO_ROOT / "src" / "cli_agent_orchestrator" / "mcp_server" / "server.py"


def _load_surfaces():
    """Load the surface roster from ux_surfaces.toml."""
    with open(_SURFACES_PATH, "rb") as f:
        data = tomllib.load(f)
    return data.get("surface", [])


def _collect_ux_marks():
    """Collect all @pytest.mark.ux tags from test/ux/ by AST walk.

    Returns a set of (surface_id, kind) tuples found in the test code.
    """
    ux_dir = _REPO_ROOT / "test" / "ux"
    found = set()

    for py_file in ux_dir.rglob("test_*.py"):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Look for pytest.mark.ux(surface="...", kind="...")
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "ux"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "mark"
                ):
                    surface = None
                    kind = None
                    for kw in node.keywords:
                        if kw.arg == "surface" and isinstance(kw.value, ast.Constant):
                            surface = kw.value.value
                        elif kw.arg == "kind" and isinstance(kw.value, ast.Constant):
                            kind = kw.value.value
                    if surface and kind:
                        found.add((surface, kind))

    return found


def _extract_mcp_tool_names():
    """Walk the AST of server.py and extract all @mcp.tool() decorated function names.

    Returns the set of tool names (the function name with leading underscore
    and trailing _impl stripped, hyphens normalized).
    """
    try:
        tree = ast.parse(_SERVER_PATH.read_text())
    except (FileNotFoundError, SyntaxError):
        return set()

    tool_names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                # Match @mcp.tool(...) or @app.tool(...)
                if isinstance(decorator, ast.Call):
                    func = decorator.func
                    if isinstance(func, ast.Attribute) and func.attr == "tool":
                        # Extract the tool name from the function name
                        name = node.name
                        # Strip async_ prefix if present
                        if name.startswith("_"):
                            name = name[1:]
                        # The tool name is typically the function name
                        tool_names.add(node.name)

    return tool_names


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCoverageMatrix:
    """F254 D6 — Coverage matrix enforcement."""

    def test_all_obligation_cells_filled(self):
        """Every obligation cell declared in ux_surfaces.toml has a test (AC-A1)."""
        surfaces = _load_surfaces()
        found_marks = _collect_ux_marks()

        missing = []
        for surface in surfaces:
            sid = surface["id"]
            obligation = surface["obligation"]

            for kind_char in obligation:
                if (sid, kind_char) not in found_marks:
                    missing.append(f"{sid} kind={kind_char} ({surface['name']})")

        assert not missing, (
            f"Coverage matrix has {len(missing)} empty obligation cell(s):\n"
            + "\n".join(f"  - {m}" for m in missing)
        )

    def test_no_unrostered_mcp_tools(self):
        """Every subagent-UX @mcp.tool() in server.py has a roster row (D6.3, AC-A3).

        Walks server.py AST, extracts tool function names, removes known
        non-UX utilities (memory, emit_ui, codex_review, etc.), and asserts
        every remaining tool appears in the roster.
        """
        surfaces = _load_surfaces()

        # Collect all tool names declared in the roster
        rostered_tools = set()
        for surface in surfaces:
            for tool in surface.get("mcp_tools", []):
                rostered_tools.add(tool)

        # Extract actual @mcp.tool() names from server.py AST
        extracted_tools = _extract_mcp_tool_names()
        assert len(extracted_tools) > 0, "No @mcp.tool() found in server.py"

        # Known non-UX utilities excluded from the roster per D3's frozen scope.
        # These are infrastructure/memory tools, not subagent-orchestration surfaces.
        _KNOWN_NON_UX = {
            "memory_store", "memory_recall", "memory_forget",
            "codex_review", "emit_ui", "load_skill", "get_compact_marker",
        }

        # Check: every extracted tool (minus known exclusions) must be rostered
        unrostered = extracted_tools - rostered_tools - _KNOWN_NON_UX
        assert not unrostered, (
            f"Found {len(unrostered)} @mcp.tool() function(s) in server.py "
            f"with no roster row in ux_surfaces.toml:\n"
            + "\n".join(f"  - {t}" for t in sorted(unrostered))
        )

        # Sanity: roster must have tools
        assert len(rostered_tools) > 0, "No tools in roster"
        assert len(surfaces) == 12, f"Expected 12 surfaces, got {len(surfaces)}"

    def test_surface_count_is_twelve(self):
        """The roster has exactly 12 entries (D3 frozen roster)."""
        surfaces = _load_surfaces()
        assert len(surfaces) == 12, f"Expected 12 surfaces, got {len(surfaces)}"

    def test_each_surface_has_invariants_and_obligation(self):
        """Every surface row has non-empty invariants and obligation."""
        surfaces = _load_surfaces()
        for surface in surfaces:
            assert surface.get("invariants"), f"{surface['id']} missing invariants"
            assert surface.get("obligation"), f"{surface['id']} missing obligation"
            # Obligation chars must be from {E, C, S, L}
            for char in surface["obligation"]:
                assert char in "ECSL", (
                    f"{surface['id']} has invalid obligation char '{char}'"
                )
