"""§4c — no module outside ``core/timing.py`` may hold a literal duration.

The blueprint states the rule and the brief asks for an AST test that enforces
it, because a retuned constant that only half the tree obeys is exactly the class
of bug the ordering invariants in ``timing.py`` exist to make loud.

Enforcing "no numeric literals at all" would be theatre — ``MAX_POLL_BYTES`` and
``_MAX_LOGGED_FAILURES`` are not durations, and a check that flagged them would
be turned off within a week.  What is checked instead is the two places a
duration can actually hide:

1. a literal handed to ``sleep`` — the only way a period gets used directly;
2. a literal assigned to a name that READS as a duration (``*_S``, ``*_MS``,
   ``*_TICKS``, ``interval``, ``timeout``, ``delay``, ``period``, ``poll``,
   ``heartbeat``) without the expression naming a ``core.timing`` constant.

Rule 2's escape hatch is deliberate: ``interval = ROLLOUT_POLL_MS / 1000.0`` is a
unit conversion, not a restated duration, and it names the constant it converts.
``interval = 0.5`` names nothing and is caught.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from cli_agent_orchestrator.core import timing

SRC = Path(__file__).resolve().parents[3] / "src" / "cli_agent_orchestrator"
NEW_PACKAGES = ("core", "adapters", "app")
TIMING_MODULE = SRC / "core" / "timing.py"

DURATION_NAME = re.compile(
    r"(^|_)(s|ms|ticks|days|interval|timeout|delay|period|poll|heartbeat|sleep|deadline)$",
    re.IGNORECASE,
)

#: An INSTANT is not a duration.  ``now_ms = int(time.time() * 1000)`` in the ULID
#: factory converts a moment into milliseconds and has nothing to do with §4c;
#: flagging it would push the next author to weaken the rule rather than obey it.
INSTANT_NAME = re.compile(r"(now|timestamp|_at)($|_)", re.IGNORECASE)

TIMING_NAMES = frozenset(name for name in timing.__all__ if name.isupper())


def _new_tree_files() -> list[Path]:
    files: list[Path] = []
    for package in NEW_PACKAGES:
        root = SRC / package
        if root.is_dir():
            files.extend(path for path in root.rglob("*.py") if path != TIMING_MODULE)
    return files


def _has_number(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Constant)
        and isinstance(sub.value, (int, float))
        and not isinstance(sub.value, bool)
        for sub in ast.walk(node)
    )


def _names(node: ast.AST) -> set[str]:
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _targets(statement: ast.AST) -> list[str]:
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return [statement.target.id]
    if isinstance(statement, ast.Assign):
        return [target.id for target in statement.targets if isinstance(target, ast.Name)]
    return []


def test_no_literal_is_slept_on() -> None:
    offenders: list[str] = []
    for path in _new_tree_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = ast.unparse(node.func)
            if not target.endswith("sleep"):
                continue
            if any(_has_number(arg) for arg in node.args):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno} {ast.unparse(node)}")
    assert offenders == []


def test_no_duration_named_binding_restates_a_number() -> None:
    offenders: list[str] = []
    for path in _new_tree_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for name in _targets(node):
                value = getattr(node, "value", None)
                if value is None or not DURATION_NAME.search(name):
                    continue
                if INSTANT_NAME.search(name):
                    continue
                if not _has_number(value):
                    continue
                if _names(value) & TIMING_NAMES:
                    continue  # a unit conversion that names what it converts
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno} {name}")
    assert offenders == []


def test_the_producers_import_their_periods_from_timing() -> None:
    """The positive half: the two timed producers name the constants, not numbers."""
    probe = (SRC / "adapters" / "truth" / "liveness_probe.py").read_text(encoding="utf-8")
    assert "PANE_HEARTBEAT_S" in probe
    assert "PANE_MISS_TICKS" in probe
    assert "PROBE_FAIL_TICKS" in probe
    rollout = (SRC / "adapters" / "truth" / "codex_rollout.py").read_text(encoding="utf-8")
    assert "ROLLOUT_POLL_MS" in rollout


def test_the_guard_catches_a_planted_violation(tmp_path: Path) -> None:
    """A check nobody has seen fail is a check nobody should trust."""
    planted = tmp_path / "planted.py"
    planted.write_text("POLL_INTERVAL_S = 0.5\n", encoding="utf-8")
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    node = tree.body[0]
    assert _targets(node) == ["POLL_INTERVAL_S"]
    assert DURATION_NAME.search("POLL_INTERVAL_S")
    assert _has_number(node.value)  # type: ignore[attr-defined]
    assert not (_names(node.value) & TIMING_NAMES)  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", sorted(TIMING_NAMES))
def test_every_timing_constant_is_a_number(name: str) -> None:
    assert isinstance(getattr(timing, name), (int, float))
