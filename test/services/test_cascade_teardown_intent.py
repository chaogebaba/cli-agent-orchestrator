"""D10 (#601) — every cascaded child gets a ``teardown.intended`` row.

``delete_terminal`` brackets only the ROOT terminal in an event-log intent (hook
7), while the cascade kills every node in the reap plan.  Without a row of its
own, the liveness probe's ``_teardown_is_live`` finds no live intent for a child
and classifies its exit ``crash`` — so an orderly reap writes a false crash into
the log this phase is about to make authoritative.

The blueprint calls it a one-line correction and rejects deferring it to phase
7's fleet read-model, on the ground that every cascaded reap in the interim would
write that false row.  A wrong row in the evidence base costs more than the fix.

Both named mutants are here: appending for the first child only (#601 survives
for the rest), and appending inside the DB-intent ``try`` (the row records what
the server INTENDED, which is true whether or not the durable intent committed).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator"
TERMINAL_SERVICE = SRC / "services" / "terminal_service.py"


def _function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(TERMINAL_SERVICE.read_text(encoding="utf-8"), filename=str(TERMINAL_SERVICE))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _intent_calls(node: ast.AST) -> list[ast.Call]:
    return [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and ast.unparse(sub.func) == "_wt_server.record_teardown_intended"
    ]


def test_the_cascade_opens_a_truth_intent_for_its_children() -> None:
    """The correction itself.  Before it, ``_open_cascade_teardown_intents`` opened
    the legacy DB intent per child and never appended the event row."""
    assert len(_intent_calls(_function("_open_cascade_teardown_intents"))) == 1


def test_the_intent_is_written_inside_the_per_child_loop() -> None:
    """The mutant: appending for the FIRST child only.

    It is invisible to a single-child test and to any test that asserts merely
    "a row was written", and it leaves #601 alive for every sibling — which on a
    real reap plan is most of them.  Asserted structurally because a behavioural
    test would need a live tmux cascade to distinguish the two.
    """
    node = _function("_open_cascade_teardown_intents")
    loops = [sub for sub in ast.walk(node) if isinstance(sub, ast.For)]
    assert loops, "the per-child loop is gone"
    inside_a_loop = any(
        any(call is inner for inner in ast.walk(loop))
        for loop in loops
        for call in _intent_calls(node)
    )
    assert inside_a_loop


def test_the_intent_is_not_inside_the_db_intent_try_block() -> None:
    """The second mutant, and the same rule hook 7 already follows.

    ``_open_cascade_teardown_intents`` continues on the in-process mark when the
    durable DB write fails — deliberately, matching ``delete_terminal``. The truth
    log has to describe what the server INTENDED, which is exactly what the probe
    needs in order not to call a healthy teardown a crash. Nesting the append in
    that ``try`` would lose the row in precisely the case the row exists for.
    """
    node = _function("_open_cascade_teardown_intents")
    calls = _intent_calls(node)
    tries = [sub for sub in ast.walk(node) if isinstance(sub, ast.Try)]
    inside_a_try = any(
        any(calls[0] is inner for inner in ast.walk(statement))
        for block in tries
        for statement in block.body
    )
    assert not inside_a_try


def test_the_child_is_its_own_scope_key_not_the_root() -> None:
    """A session-scope intent would cover the subtree in one row and also suppress
    the ERROR for siblings on the same session that are NOT being deleted, which
    is the safety property F716's own AC-2 protects.  The intents are per-terminal,
    exactly over the reap plan, and the truth row must say the same thing the
    legacy intent says.
    """
    call = _intent_calls(_function("_open_cascade_teardown_intents"))[0]
    assert ast.unparse(call.args[0]) == "node_id"
    keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
    assert keywords["scope_kind"] == "'terminal'"
    assert keywords["scope_key"] == "node_id"


def test_hook_7_in_delete_terminal_is_untouched() -> None:
    """The root's own row still exists and is still exactly one.

    D10 adds a row for the children; it must not move or duplicate the one
    ``delete_terminal`` already writes, or a root reap would appear twice in a log
    whose whole value is that a repeat means something.
    """
    assert len(_intent_calls(_function("delete_terminal"))) == 1
