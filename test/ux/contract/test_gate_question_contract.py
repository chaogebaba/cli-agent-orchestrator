"""S13 contract — the durable-question tools and their CLI twins (slice B1).

Invariant #535/F680: every new MCP tool ships with a ``cao gate`` verb over the
IDENTICAL route.  What that buys is checked here by construction rather than by
comparing behaviour twice: both surfaces name the same path constant, so there is
one service call underneath and no way for the two to drift into meaning
different things.

Rostered as S13 rather than folded into S09 (``answer_user_prompt``): that tool
answers a PROVIDER DIALOG in somebody's pane, ``answer_question`` settles a
durable gate row, and recording the two as one surface would record two different
waits as one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SERVER = _ROOT / "src" / "cli_agent_orchestrator" / "mcp_server" / "server.py"
_CLI = _ROOT / "src" / "cli_agent_orchestrator" / "cli" / "commands" / "gate.py"


def _tool_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                func = getattr(decorator, "func", None)
                if isinstance(func, ast.Attribute) and func.attr == "tool":
                    names.add(node.name)
    return names


@pytest.mark.ux(surface="S13", invariant="UX-3", kind="C")
def test_both_question_tools_exist_and_are_rostered() -> None:
    names = _tool_names(_SERVER)
    assert {"ask_supervisor", "answer_question"} <= names


@pytest.mark.ux(surface="S13", invariant="UX-3", kind="C")
def test_every_question_tool_has_a_cao_gate_verb_over_the_same_route() -> None:
    """#535/F680, checked as a property of the source rather than of a run."""
    server = _SERVER.read_text()
    cli = _CLI.read_text()
    for route in ('"/gate/questions"', "/gate/questions/{question_id}/answer"):
        assert route.strip('"') in server, f"MCP surface does not reach {route}"
    assert '"/gate/questions"' in cli
    assert "/gate/questions/{question_id}/answer" in cli
    for verb in ('@gate.command("ask")', '@gate.command("answer")'):
        assert verb in cli


@pytest.mark.ux(surface="S13", invariant="UX-3", kind="C")
def test_the_non_blocking_tool_form_demands_a_default_answer() -> None:
    """The B1 form returns at once, so the caller must say what it proceeds with.

    Checked at the tool signature because that is where the obligation has to be
    visible to an agent reading the schema; the domain rule underneath it is
    ``core.gate.validate_ask``.
    """
    tree = ast.parse(_SERVER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "ask_supervisor":
            args = {a.arg for a in node.args.args + node.args.kwonlyargs}
            assert "default_answer" in args
            assert "blocking" not in args, (
                "the blocking wait is slice B2; a blocking flag with no wait loop "
                "would promise a suspension the tool does not perform"
            )
            return
    pytest.fail("ask_supervisor not found in the MCP server")
