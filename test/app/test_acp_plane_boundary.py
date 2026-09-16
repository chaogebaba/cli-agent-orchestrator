"""AC-S1.9 — the plane is INFRASTRUCTURE, and the boundary is a grep.

D0 is the decision: the message plane knows terminals, envelopes, conditions,
transport outcomes and the routing table, and has **zero** knowledge of gates,
reviews, verdicts, digests or certification-as-routing-input policy.  Two modes
sit on top of it and BARE is the one that must always work.

The check is a grep because the boundary is not enforceable by imports.  Nothing
stops ``app/acp/`` from growing a string that says ``verdict``; what stops it is
that this test goes red.  And the cost of losing the boundary is not a broken
build — it is that a lightweight project running the same CAO starts needing the
doctrine to be present, which is precisely what the architecture boundary in the
root CLAUDE.md forbids.

``position`` and ``routing`` are deliberately NOT in the list (review N5).  Under
the routing ruling a correct BARE ``assign`` schema contains both, so the r2 form
of this AC would have failed a correct build — and an AC that fails the design it
is checking is worse than no AC.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import cli_agent_orchestrator

_SRC = Path(cli_agent_orchestrator.__file__).resolve().parent

#: The five forbidden vocabularies, as whole words.  Substring matching would
#: flag ``aggregate`` for ``gate`` and ``reviewer_id`` would be the least of it.
_FORBIDDEN = ("gate", "gates", "review", "reviews", "verdict", "verdicts", "digest", "digests")

#: ``certification`` is not forbidden outright: D13's certification ROW is
#: infrastructure and D23b says the cell guard fires in BARE too.  What is
#: forbidden is certification as ROUTING-INPUT POLICY — the thing that decides
#: which cells matter — so the pattern names the policy, not the noun.
_FORBIDDEN_POLICY = re.compile(r"certification[_\- ]?policy", re.IGNORECASE)

_PLANE_PACKAGES = ("adapters/acp", "app/acp")

#: Explicitly allowed anywhere in the plane, because they are NOT the doctrine's
#: vocabulary: ``aggregate`` (the store pattern), ``delegate``/``delegated``,
#: ``navigate``, ``investigate``, ``mitigate``, ``propagate``.  Listed so the
#: word-boundary regex below has something to be checked against.
_INNOCENT = ("aggregate", "delegate", "navigate", "investigate", "mitigate", "propagate")


def _plane_files() -> list[Path]:
    files: list[Path] = []
    for package in _PLANE_PACKAGES:
        directory = _SRC / package
        assert directory.exists(), f"{package} must exist for this AC to mean anything"
        files.extend(sorted(directory.rglob("*.py")))
    return files


def _hits(text: str) -> list[str]:
    found: list[str] = []
    for word in _FORBIDDEN:
        if re.search(rf"\b{word}\b", text, flags=re.IGNORECASE):
            found.append(word)
    if _FORBIDDEN_POLICY.search(text):
        found.append("certification_policy")
    return found


def _surface_text(path: Path) -> str:
    """Everything the plane IS or EMITS: identifiers and runtime strings.

    Docstrings and comments are deliberately excluded, and the AC's own wording
    is why — it forbids an *identifier*, not a sentence.  The distinction is not
    a loophole: this package has to be able to SAY what it is not allowed to
    know, and citing the review round that settled a decision is how a reader
    finds the argument.  A check that forbade the explanation would push the
    reasoning out of the file and leave only the rule.

    Runtime string constants ARE included, because §11's kill list names
    "doctrine-flavoured strings the plane puts in front of a seat" as the largest
    hidden cost in this WP.  A docstring is not put in front of a seat; a
    message is.
    """
    tree = ast.parse(path.read_text())
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(node.name)
        elif isinstance(node, ast.arg):
            parts.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            parts.append(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                parts.append(node.value)
    return "\n".join(parts)


def test_the_plane_names_no_doctrine_identifier() -> None:
    """Identifiers and runtime strings across both plane packages."""
    offenders: list[str] = []
    for path in _plane_files():
        hits = _hits(_surface_text(path))
        if hits:
            offenders.append(f"{path.relative_to(_SRC)}: {sorted(set(hits))}")
    assert not offenders, "doctrine vocabulary inside the plane (AC-S1.9):\n" + "\n".join(
        offenders
    )


def test_position_and_routing_are_not_in_the_forbidden_list() -> None:
    """Review N5, as a test of the TEST.

    The r2 form of this AC listed ``position`` and ``routing``.  Under the
    routing ruling a correct BARE ``assign`` schema contains both, so that form
    would have failed a correct build — the AC's own fails-if names it.
    """
    for allowed in ("position", "routing"):
        assert allowed not in _FORBIDDEN
        assert not _hits(f"the {allowed} resolves at the MCP edge")


@pytest.mark.parametrize("word", _INNOCENT)
def test_the_grep_does_not_fire_on_an_innocent_word(word: str) -> None:
    """Word boundaries, not substrings: ``aggregate`` contains ``gate``."""
    assert not _hits(f"one {word} owns the transaction")


@pytest.mark.parametrize("word", ["gate", "review", "verdict", "digest"])
def test_the_grep_would_catch_a_real_violation(word: str) -> None:
    assert _hits(f"the {word} decides whether to deliver")


def test_certification_as_a_noun_is_allowed_but_as_policy_is_not() -> None:
    """D13's row is infrastructure; the policy that reads it is not.

    D23b is explicit that the cell guard fires in BARE too — it is a typed
    refusal reason, which is transport vocabulary — while the policy deciding
    which cells matter is SKILL.  So the pattern names the policy.
    """
    assert not _hits("the certification row records resume: none")
    assert "certification_policy" in _hits("consult the certification policy")
    assert "certification_policy" in _hits("consult the certification_policy module")


def test_the_bare_tool_schemas_carry_no_doctrine_vocabulary() -> None:
    """The third surface AC-S1.9 names: the BARE MCP tool schemas and descriptions.

    A BARE seat reads these five descriptions and nothing else.  A description
    that told it about review rounds would have handed it the doctrine through
    the one channel the mode exists to keep clean.
    """
    from cli_agent_orchestrator.mcp_server.server import BARE_MODE_TOOLS

    source = (_SRC / "mcp_server" / "server.py").read_text()
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tool_name = _tool_name(node)
        if tool_name not in BARE_MODE_TOOLS:
            continue
        rendered = _schema_text(node)
        hits = _hits(rendered)
        if hits:
            offenders.append(f"{tool_name}: {sorted(set(hits))}")
    assert not offenders, "doctrine vocabulary in a BARE tool schema:\n" + "\n".join(offenders)


def _tool_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The MCP tool name for a decorated function, honouring ``name=``."""
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call is not None else decorator
        if not (isinstance(target, ast.Attribute) and target.attr == "tool"):
            continue
        if call is not None:
            for keyword in call.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    return str(keyword.value.value)
        return node.name
    return None


def _schema_text(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """The parameter descriptions and the docstring — what a client actually reads.

    Deliberately NOT the function body: the boundary is about what the plane
    TELLS a bare seat, and an internal variable name is not part of the schema.
    """
    parts: list[str] = [ast.get_docstring(node) or ""]
    for default in list(node.args.defaults) + list(node.args.kw_defaults):
        if default is None:
            continue
        for sub in ast.walk(default):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                parts.append(sub.value)
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                for sub in ast.walk(keyword.value):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        parts.append(sub.value)
    return "\n".join(parts)


def test_the_planes_tests_pass_with_the_skill_absent() -> None:
    """The AC's second clause, checked structurally.

    Nothing under the plane imports the doctrine loader or the orchestrator
    skill, so removing them cannot change whether these tests pass.  A dynamic
    version of this would have to delete files from the checkout, which is not a
    thing a test may do.
    """
    for path in _plane_files():
        text = _surface_text(path)
        assert "load_skill" not in text, path
        assert "orchestrator/doctrine" not in text, path
        assert "/orchestrator" not in text, path
