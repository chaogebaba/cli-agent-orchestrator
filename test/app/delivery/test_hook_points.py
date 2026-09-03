"""The five phase-3a hook points, asserted structurally (F728 #584).

Read with :mod:`ast` and by reading the source, rather than by exercising the
behaviour, for the reason phase 1's equivalent gives: what a hook-point contract
promises is a property of the DIFF, not of a run.  A behavioural test cannot
distinguish "the shadow write is post-commit" from "the shadow write happened to
succeed inside the transaction this time", and the difference between those two
is a lock contention and a shadow row for a rolled-back message.

The list is deliberately short, and keeping it short is the contract: five call
sites, three legacy files, one legacy module that names the new tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "cli_agent_orchestrator"

#: The only legacy functions that may call into the mirror, and what each is for.
HOOK_POINTS = {
    ("clients/database.py", "_create_inbox_message_unfenced"): "record_inbox_row",
    ("services/mailbox_service.py", "_create_logical_inbox_message_inner"): "record_inbox_row",
    ("clients/database.py", "settle_delivery_attempt"): "observe_messages",
    ("clients/database.py", "write_through_terminal_state"): "_stash_shadow_observation",
    ("services/inbox_service.py", "deliver_pending"): "observe_veto",
    # 4b, 4c and 4d: the three CANCEL writers. None of them passes through
    # write_through_terminal_state, so without these three a cancelled row's
    # shadow copy would sit ready and read as a legacy-early disagreement for the
    # life of the queue. They stash rather than call, like hook point 4, because
    # each runs inside its caller's transaction.
    ("clients/database.py", "delete_terminal_and_warm_intent"): "_stash_shadow_observation",
    ("clients/database.py", "_close_barrier_owner_gone_in_db"): "_stash_shadow_observation",
    ("clients/database.py", "cancel_pending_watchdog_message"): "_stash_shadow_observation",
}


def _function(path: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse((SRC / path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path} has no function {name}")


def _calls(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def test_every_hook_point_is_where_the_contract_says_it_is() -> None:
    for (path, function), call in HOOK_POINTS.items():
        assert call in _calls(_function(path, function)), f"{path}:{function} lost its {call} call"


def test_no_other_legacy_function_calls_the_mirror() -> None:
    """The contact surface stays five sites, so a reviewer can enumerate it.

    A sixth hook added without this test failing is how the "one file to read"
    property quietly stops being true — and phase 3a's whole argument for a
    bridge module is that the surface is small enough to read.
    """
    entry_points = {"record_inbox_row", "observe_messages", "observe_veto"}
    found: set[tuple[str, str]] = set()
    for directory in ("clients", "services", "utils", "backends", "providers"):
        package = SRC / directory
        if not package.exists():
            continue
        for file in package.rglob("*.py"):
            if file.name == "delivery_mirror.py":
                continue
            tree = ast.parse(file.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for call in _calls(node) & entry_points:
                        found.add((str(file.relative_to(SRC)), node.name))
    expected = {
        (path, function)
        for (path, function), call in HOOK_POINTS.items()
        if call in {"record_inbox_row", "observe_messages", "observe_veto"}
    }
    # The after-commit drain is a sixth CALLER of observe_messages, and it is
    # named here rather than in HOOK_POINTS because it is not a hook point: it is
    # the mechanism hook point 4 uses to reach post-commit at all.
    expected.add(("clients/database.py", "_wp_arch_p3a_after_commit"))
    assert found == expected


def test_the_enqueue_hooks_run_after_the_commit_they_observe() -> None:
    """The property that cannot be tested behaviourally, so it is read instead.

    Both enqueue hooks must sit AFTER ``db.commit()`` in their own function. The
    queue's store holds a second connection to the same SQLite file, so a call
    before the commit would contend for the write lock the caller is holding and
    would leave a shadow row behind for a legacy row that then rolled back.
    """
    for path, function in (
        ("clients/database.py", "_create_inbox_message_unfenced"),
        ("services/mailbox_service.py", "_create_logical_inbox_message_inner"),
    ):
        node = _function(path, function)
        commit_lines = [
            child.lineno
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "commit"
        ]
        hook_lines = [
            child.lineno
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "record_inbox_row"
        ]
        assert commit_lines and hook_lines, f"{path}:{function}"
        assert min(hook_lines) > min(
            commit_lines
        ), f"{path}:{function} calls the mirror before its commit"


def test_the_settle_hook_runs_outside_its_transaction_block() -> None:
    """``settle_delivery_attempt`` opens ``with SessionLocal.begin()``.

    The mirror call must sit outside that block, or it re-reads a status that has
    not committed and contends with the transaction that is about to write it.
    """
    node = _function("clients/database.py", "settle_delivery_attempt")
    with_nodes = [child for child in node.body if isinstance(child, ast.With)]
    assert with_nodes, "settle_delivery_attempt no longer opens a with-block"
    inside = _calls(with_nodes[0])
    assert "observe_messages" not in inside
    assert "observe_messages" in _calls(node)


def test_only_the_bridge_module_names_the_new_tree() -> None:
    """Three legacy files gained a call; one legacy file gained an import.

    That is lane C's phase-1 pattern applied again, and the property it buys is
    that a reviewer asking "what did phase 3a attach to the running server" reads
    one file rather than grepping the two largest legacy packages.
    """
    markers = ("cli_agent_orchestrator.app", "cli_agent_orchestrator.core")
    for path in ("clients/database.py", "services/mailbox_service.py"):
        text = (SRC / path).read_text()
        for marker in markers:
            assert marker not in text, f"{path} now names the new tree directly"
    bridge = (SRC / "services" / "delivery_mirror.py").read_text()
    assert "cli_agent_orchestrator.app.delivery" in bridge
