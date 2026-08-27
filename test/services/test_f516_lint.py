"""F516 AC7 lint: F516 test files use only the public AutoResponder surface.

Scope (r2-S7): files matching ``test/services/test_f516_*.py`` ONLY (a path-
convention baseline; the pre-existing direct-access tests in
test_f55_auto_responder_hardening.py / test_auto_responder.py are exempt by
construction). Asserts no attribute access on underscore-prefixed AutoResponder
members within that scope, so the new tests exercise the public seams
(match_verdict, on_screen, waiting_gate, clear_terminal, …) and cannot ossify
private internals the train is actively reshaping.

Module-level globals (ar._store) and other modules' private helpers
(draft_guard._consult_dialog_before_send) are NOT AutoResponder members and are
out of scope; the check keys on receivers whose static name suggests an
AutoResponder instance (``engine``/``responder``/``AutoResponder(...)``).
"""

import ast
from pathlib import Path

SCOPE_GLOB = "test_f516_*.py"
_AR_RECEIVER_NAMES = {"engine", "responder", "auto_responder", "ar_engine"}


def _f516_test_files():
    here = Path(__file__).parent
    return sorted(p for p in here.glob(SCOPE_GLOB) if p.name != Path(__file__).name)


def _receiver_is_autoresponder(node: ast.AST) -> bool:
    # engine._x / responder._x
    if isinstance(node, ast.Name) and node.id in _AR_RECEIVER_NAMES:
        return True
    # AutoResponder()._x
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id == "AutoResponder"
    return False


def test_f516_test_files_do_not_touch_underscore_autoresponder_members():
    offenders = []
    for path in _f516_test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for attr in ast.walk(tree):
            if not isinstance(attr, ast.Attribute):
                continue
            if not attr.attr.startswith("_"):
                continue
            if _receiver_is_autoresponder(attr.value):
                offenders.append(f"{path.name}:{attr.lineno} .{attr.attr}")
    assert not offenders, (
        "F516 test files must use only the public AutoResponder surface; "
        f"underscore-member access found: {offenders}"
    )


def test_lint_scope_is_non_empty():
    # Guard against the glob silently matching nothing (the lint would pass
    # vacuously and never catch a regression).
    assert _f516_test_files(), "no test_f516_*.py files found in scope"
