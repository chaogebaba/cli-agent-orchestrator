"""§4c — timing constants and their ordering invariants (WP-ARCH phase 1, F725 #581).

These tests import the constants and never the numbers, which is the blueprint's
own rule.  The one place a literal appears is the "no literal duration anywhere
else" sweep at the bottom, whose whole job is to notice literals.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from cli_agent_orchestrator.core import timing


def test_orderings_hold() -> None:
    """The module checks itself at import; assert the checker is callable and green."""
    timing.check_orderings()


def test_rollout_polls_several_times_per_heartbeat() -> None:
    assert timing.ROLLOUT_POLL_MS * 4 <= timing.PANE_HEARTBEAT_S * 1000


def test_one_missed_probe_never_degrades() -> None:
    assert timing.NO_SIGNAL_S > timing.PANE_HEARTBEAT_S * 2


def test_fleet_wide_producer_error_is_never_faster_than_one_terminal_silence() -> None:
    assert timing.PROBE_FAIL_TICKS * timing.PANE_HEARTBEAT_S >= timing.NO_SIGNAL_S


def test_one_miss_never_exits_a_process() -> None:
    assert timing.PANE_MISS_TICKS >= 2


def test_retention_sweep_is_slower_than_the_heartbeat() -> None:
    assert timing.RETENTION_SWEEP_S >= timing.PANE_HEARTBEAT_S


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PANE_HEARTBEAT_S", 20),
        ("NO_SIGNAL_S", 60),
        ("ROLLOUT_POLL_MS", 500),
        ("PROBE_FAIL_TICKS", 3),
        ("PANE_MISS_TICKS", 2),
        ("RETENTION_DAYS", 30),
    ],
)
def test_values_match_the_blueprint_table(name: str, value: int) -> None:
    """The §4c table is normative; this is the one place its numbers are written."""
    assert getattr(timing, name) == value


def test_check_orderings_would_reject_a_bad_retune(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checker is real: loosen one constant and it raises.

    Without this, ``check_orderings`` could be a no-op and every ordering test
    above would still pass.
    """
    monkeypatch.setattr(timing, "NO_SIGNAL_S", timing.PANE_HEARTBEAT_S)
    with pytest.raises(ValueError, match="NO_SIGNAL_S"):
        timing.check_orderings()


_NEW_PACKAGES = ("core", "adapters", "app")
_DURATION_SUFFIXES = ("_S", "_MS", "_SECONDS", "_DAYS", "_TICKS")


def test_no_other_new_module_defines_a_duration_constant() -> None:
    """§4c: no module outside ``core/timing.py`` may hold a literal duration.

    An AST sweep over the new packages for module-level assignments whose name
    looks like a duration.  It cannot catch a duration hidden in an expression,
    but it catches the way this rule is actually broken — someone re-declaring
    ``PANE_HEARTBEAT_S = 20`` next to the code that uses it.
    """
    src = Path(timing.__file__).resolve().parents[1]
    offenders: list[str] = []
    for package in _NEW_PACKAGES:
        package_dir = src / package
        if not package_dir.exists():
            continue
        for path in package_dir.rglob("*.py"):
            if path.resolve() == Path(timing.__file__).resolve():
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in tree.body:
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.endswith(_DURATION_SUFFIXES):
                        offenders.append(f"{path.relative_to(src)}:{node.lineno}:{target.id}")
    assert not offenders, "duration constants outside core/timing.py:\n" + "\n".join(offenders)
