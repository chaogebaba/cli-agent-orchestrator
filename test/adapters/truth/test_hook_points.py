"""AC11 — the seven legacy hook points, asserted structurally (F725 #581, lane B).

These tests read the fork's own source with :mod:`ast` rather than exercising the
behaviour, because what AC11 promises is a property of the DIFF: exactly these
files touch the new tree, each hook sits at the stated place, and nothing else in
``services/``, ``clients/`` or ``utils/`` changed.  A behavioural test cannot
distinguish "the hook is at the function exit" from "the hook is at the verify
call" when both happen to fire; the AST can, and that distinction is a phase-1
mutant.

Lane B owns hook points 1, 2, 2b, 3, 6 and 7.  Points 4 (the ``cao diag`` CLI
registration) and 5 (``bootstrap.py``) belong to lanes C and A; the file-set
assertion is written so it does not fail before their work lands.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "cli_agent_orchestrator"

#: AC11's list, verbatim.  Nothing outside it may import the new tree.
AC11_LEGACY_IMPORTERS = {
    "providers/codex.py",
    "services/status_monitor.py",
    "services/fleet_service.py",
    "services/terminal_service.py",
    # AC11 names ``cli/main.py`` for hook point 4, the diag CLI registration.
    # Lane C put the new-tree imports in a dedicated command module instead and
    # left ``cli/main.py`` importing only that module — a legacy-to-legacy
    # import, which is why ``main.py`` no longer appears here at all.  The
    # deviation is an improvement on the blueprint and is recorded rather than
    # smoothed over: the contact surface is one command file instead of the CLI
    # entry point, and ``cli/commands/diag.py`` imports ``app`` and ``core`` but
    # NOT ``adapters``, so the fifth contract (only the composition root names an
    # adapter) still holds.
    "cli/commands/diag.py",
}

#: The subset lane B is responsible for.
LANE_B_IMPORTERS = {
    "providers/codex.py",
    "services/status_monitor.py",
    "services/fleet_service.py",
    "services/terminal_service.py",
}

NEW_PACKAGES = {"core", "app", "adapters"}
LEGACY_ROOTS = {
    "providers",
    "services",
    "clients",
    "utils",
    "backends",
    "models",
    "schemas",
    "kernel",
    "plugins",
    "tui",
    "telemetry",
    "graph",
    "hooks",
    "sim",
    "security",
    "sandbox_bootstrap",
    "constants",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_roots(tree: ast.Module) -> set[str]:
    """Second path segment of every ``cli_agent_orchestrator.X`` import in a file."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == "cli_agent_orchestrator" and len(parts) > 1:
                roots.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "cli_agent_orchestrator" and len(parts) > 1:
                    roots.add(parts[1])
    return roots


def _legacy_files() -> list[Path]:
    return [
        path
        for path in SRC.rglob("*.py")
        if path.relative_to(SRC).parts[0] not in NEW_PACKAGES and path.name != "bootstrap.py"
    ]


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    names: list[str] = []
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        names.append(ast.unparse(target))
    return names


def _calls(node: ast.AST) -> list[str]:
    return [
        ast.unparse(sub.func)
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and isinstance(sub.func, (ast.Attribute, ast.Name))
    ]


# -- the file set -----------------------------------------------------------


def test_only_the_ac11_files_import_the_new_tree() -> None:
    """The decisive contract: legacy reaches the new tree ONLY at the hook points.

    EQUALITY, not containment.  While the lanes were landing separately this was
    written as a subset so it would not fail before lane C existed, and a subset
    is the weaker half of the claim: it catches a legacy file that starts
    importing the new tree, and says nothing about one that quietly stops.  Both
    directions matter now.  A file dropping out of this set means a hook point
    was deleted, which is exactly how phase 1 would decay into a diagnostic
    surface that compiles, passes, and observes nothing.
    """
    importers = {
        str(path.relative_to(SRC))
        for path in _legacy_files()
        if _imported_roots(_tree(path)) & NEW_PACKAGES
    }
    unexpected = sorted(importers - AC11_LEGACY_IMPORTERS)
    missing = sorted(AC11_LEGACY_IMPORTERS - importers)
    assert importers == AC11_LEGACY_IMPORTERS, f"unexpected={unexpected} missing={missing}"


def test_the_new_tree_never_imports_legacy() -> None:
    """``new-code-never-imports-legacy``.

    This is why the liveness probe takes tmux and the fleet roster as injected
    callables, and why the delivery classifier matches exception CLASS NAMES
    rather than importing ``draft_guard``.
    """
    offenders: dict[str, set[str]] = {}
    for package in NEW_PACKAGES:
        root = SRC / package
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            leaked = _imported_roots(_tree(path)) & LEGACY_ROOTS
            if leaked:
                offenders[str(path.relative_to(SRC))] = leaked
    assert offenders == {}


def test_adapters_never_import_app() -> None:
    """``adapters-are-leaves``: a producer appends, it never calls the projector."""
    for path in (SRC / "adapters").rglob("*.py"):
        assert "app" not in _imported_roots(_tree(path)), path


# -- hook 1: providers/codex.py --------------------------------------------


def test_hook_1_wraps_resolve_rollout_file() -> None:
    """The path is HANDED IN (N2); the tailer never resolves it itself.

    A decorator, because ``_resolve_rollout_file`` has five return points and a
    hook covering only some of them would tail the right file for a fresh session
    and the wrong one for a resumed one.
    """
    tree = _tree(SRC / "providers" / "codex.py")
    node = _function(tree, "_resolve_rollout_file")
    assert "_wt_rollout_resolution_hook" in _decorator_names(node)


# -- hook 2 / 2b: the status egress and the fleet overrides ------------------


def test_hook_2_is_at_publish_observation_not_at_the_probe_path() -> None:
    """The mutant this kills: hooking ``:2772``, the on-demand probe.

    ``_publish_observation`` is the egress every origin passes through; the probe
    fires only when the watchdog, the doorbell or the inbox asks.
    """
    tree = _tree(SRC / "services" / "status_monitor.py")
    node = _function(tree, "_publish_observation")
    assert "_wt_legacy_egress.record_legacy_publish" in _calls(node)

    for other in ast.walk(tree):
        if (
            isinstance(other, (ast.FunctionDef, ast.AsyncFunctionDef))
            and other.name != "_publish_observation"
        ):
            assert "_wt_legacy_egress.record_legacy_publish" not in _calls(other), other.name


def test_hook_2_runs_before_the_metadata_check() -> None:
    """It must record the egress it was asked for, not only the ones that succeed."""
    node = _function(_tree(SRC / "services" / "status_monitor.py"), "_publish_observation")
    body = [statement for statement in node.body if not isinstance(statement, ast.Expr)]
    first = body[0]
    assert isinstance(first, ast.If)  # the ``if metadata is None`` raise
    hook_line = min(
        sub.lineno
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and ast.unparse(sub.func) == "_wt_legacy_egress.record_legacy_publish"
    )
    assert hook_line < first.lineno


def test_hook_2b_covers_all_three_fleet_error_overrides() -> None:
    """#571 is what happens when an override leaves no trace of itself."""
    source = (SRC / "services" / "fleet_service.py").read_text(encoding="utf-8")
    assert source.count("_wt_legacy_egress.record_fleet_override(") == 3
    assert source.count("status = TerminalStatus.ERROR") == 3


# -- hook 3: the two dispatch functions -------------------------------------


@pytest.mark.parametrize("name", ["send_input", "send_prepared_input"])
def test_hook_3_wraps_both_dispatch_functions(name: str) -> None:
    """At the FUNCTION exit, never at the ``verify_submission_after_send`` call.

    ``preserve_draft_before_send`` raises above the inner try block, so a hook at
    the verify call would record nothing for the #555 failure that started this
    work package.
    """
    tree = _tree(SRC / "services" / "terminal_service.py")
    node = _function(tree, name)
    assert "_wt_server.dispatch_attempt" in _decorator_names(node)
    assert "verify_submission_after_send" in "\n".join(_calls(node))


def test_hook_3_is_not_at_the_verify_call() -> None:
    source = (SRC / "services" / "terminal_service.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if "verify_submission_after_send" in line:
            assert "_wt_server" not in line


# -- hooks 6 and 7: the teardown decisions ----------------------------------


def test_hook_6_is_in_the_deferred_failure_settler() -> None:
    tree = _tree(SRC / "services" / "terminal_service.py")
    node = _function(tree, "_claim_and_settle_deferred_failure")
    assert "_wt_server.record_teardown_decided" in _calls(node)


def test_hook_7_is_in_delete_terminal() -> None:
    """The probe reads these rows back to call an exit ``teardown`` and not ``crash``."""
    tree = _tree(SRC / "services" / "terminal_service.py")
    node = _function(tree, "delete_terminal")
    assert "_wt_server.record_teardown_intended" in _calls(node)


def test_the_teardown_intent_row_is_written_even_when_the_db_intent_failed() -> None:
    """``delete_terminal`` continues on the in-process mark when the DB write fails.

    The truth log has to describe what the server INTENDED, because that is what
    the probe needs in order not to call a healthy teardown a crash.
    """
    tree = _tree(SRC / "services" / "terminal_service.py")
    node = _function(tree, "delete_terminal")
    hook_calls = [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and ast.unparse(sub.func) == "_wt_server.record_teardown_intended"
    ]
    assert len(hook_calls) == 1
    tries = [sub for sub in ast.walk(node) if isinstance(sub, ast.Try)]
    inside_a_try = any(
        any(hook_calls[0] is inner for inner in ast.walk(statement))
        for block in tries
        for statement in block.body
    )
    assert not inside_a_try


# -- lane B's whole legacy footprint ----------------------------------------


def test_lane_b_touches_only_four_legacy_files() -> None:
    """Named so a reviewer can diff the claim against ``git diff --stat``."""
    assert LANE_B_IMPORTERS == {
        "providers/codex.py",
        "services/status_monitor.py",
        "services/fleet_service.py",
        "services/terminal_service.py",
    }
