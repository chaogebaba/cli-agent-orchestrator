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


# --------------------------------------------- the pane-sample cadence (2b)


def test_the_staleness_mirror_matches_the_legacy_sampler() -> None:
    """The one constant ``core`` cannot import, so drift is caught here instead.

    ``PANE_SAMPLE_S`` is chosen against this horizon: the liveness probe becomes
    the pane sample's only driver once phase 3 deletes the stalled-callback
    watchdog, and a drive slower than half the staleness window hands
    ``fuse_status``'s rules 3a/3b a stale sample for part of every window — which
    reads to them as NO evidence and silently disables the pane-delta downgrade.
    A retune that moved one number and not the other would leave both files
    looking correct on their own.
    """
    from cli_agent_orchestrator.services.pane_liveness import _STALENESS_S

    assert timing.PANE_LIVENESS_STALENESS_S == _STALENESS_S


def test_the_sample_cadence_covers_the_staleness_window() -> None:
    """Stated over the constants, so a retune fails here rather than at 3am."""
    timing.check_orderings()

    assert timing.PANE_SAMPLE_S * 2 <= timing.PANE_LIVENESS_STALENESS_S
    assert timing.PANE_SAMPLE_S < timing.PANE_HEARTBEAT_S
    assert timing.PANE_HEARTBEAT_S % timing.PANE_SAMPLE_S == 0


# ------------------------------------- Seam B's submission wait (WP-HERDR H2)


def test_the_seam_b_wait_is_bracketed_by_herdr_s_gate_and_the_inject_budget() -> None:
    """B1/B2 as a statement, not only as a checker arm.

    Both bounds change what a delivery OUTCOME means.  Under the lower one every
    stall is reported as the coarser ``timeout``; over the upper one a single
    submission outlives the round-trip its lease was sized for.
    """
    assert timing.HERDR_SUBMISSION_GATE_MS < timing.HERDR_PROMPT_WAIT_MS
    assert timing.HERDR_PROMPT_WAIT_MS <= timing.DELIVERY_INJECT_BUDGET_S * 1000


def test_check_delivery_orderings_rejects_a_wait_below_herdr_s_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The B1 arm is real: drop the wait under the gate and it raises."""
    monkeypatch.setattr(timing, "HERDR_PROMPT_WAIT_MS", timing.HERDR_SUBMISSION_GATE_MS)
    with pytest.raises(ValueError, match="B1"):
        timing.check_delivery_orderings()


def test_check_delivery_orderings_rejects_a_wait_over_the_inject_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And the B2 arm."""
    monkeypatch.setattr(timing, "HERDR_PROMPT_WAIT_MS", timing.DELIVERY_INJECT_BUDGET_S * 1000 + 1)
    with pytest.raises(ValueError, match="B2"):
        timing.check_delivery_orderings()


def test_herdr_s_gate_is_the_number_herdr_documents() -> None:
    """A MIRRORED herdr fact, re-certified with the protocol pin.

    ``herdr agent prompt --help`` on 0.9.0: "--wait requires an observed working
    or blocked state within 5000ms; otherwise it returns agent_prompt_stalled".
    Pinned here so a herdr bump that moves it cannot pass silently.
    """
    assert timing.HERDR_SUBMISSION_GATE_MS == 5000
# ------------------------------------------ WP-ACP-PLANE S1: the nine frozen
# literals (AC-S1.24) and the interrupt orderings, with one named mutant per
# ordering.  The mutants are the point: an ordering nobody can break is
# documentation, and r4's blocker on AC-S1.17 was exactly an assertion that
# could not fail.


@pytest.mark.parametrize(
    ("name", "value"),
    [
        # AC-S1.24's nine, FROZEN 2026-09-16 against s0-round2.md.  Written out
        # here so a retune has to edit a test that names the freeze, not just a
        # constant.  "A constant is changed after the AC that uses it was
        # signed" is AC-S1.24's fails-if, and this is the tripwire for it.
        ("INTERRUPT_MIN_GAP_S", 30),
        ("INTERRUPT_BUDGET_N", 5),
        ("INTERRUPT_BUDGET_WINDOW_S", 600),
        ("INTERRUPT_MAX_LATENCY_S", 10),
        ("ACP_CANCEL_SETTLE_S", 20),
        ("RECOVERY_DEADLINE_S", 120),
        ("CANCEL_HOLD_MARGIN_S", 15),
        ("ACP_WRITE_SETTLE_S", 1),
        ("ACP_KILL_GRACE_S", 3),
    ],
)
def test_the_nine_frozen_interrupt_literals(name: str, value: int) -> None:
    assert getattr(timing, name) == value


def test_exactly_nine_literals_were_frozen() -> None:
    """AC-S1.24 says NINE.  A tenth added without a freeze is the drift."""
    frozen = {
        "INTERRUPT_MIN_GAP_S",
        "INTERRUPT_BUDGET_N",
        "INTERRUPT_BUDGET_WINDOW_S",
        "INTERRUPT_MAX_LATENCY_S",
        "ACP_CANCEL_SETTLE_S",
        "RECOVERY_DEADLINE_S",
        "CANCEL_HOLD_MARGIN_S",
        "ACP_WRITE_SETTLE_S",
        "ACP_KILL_GRACE_S",
    }
    assert len(frozen) == 9
    assert frozen <= set(timing.__all__)


def test_the_interrupt_orderings_hold() -> None:
    timing.check_delivery_orderings()


# Each row is (label, constant, replacement) — the replacement is the SMALLEST
# change that violates that one ordering, so a mutant cannot pass by tripping a
# neighbouring check first.
_ORDERING_MUTANTS = [
    # AC-S1.17 (1): move the legacy stall age without moving the credit.
    ("I4-credit", "IDLE_STALL_AGE_S", 1801),
    # AC-S1.17 (1) again, from the other side: a MAX_LIFETIME move.
    ("I4-credit", "DELIVERY_MAX_LIFETIME_S", 1699),
    # AC-S1.17 (2): a zero margin satisfies the equality and still ties.
    ("I4-margin", "BUSY_CREDIT_MARGIN_S", 0),
    ("I4-cap", "BUSY_CREDIT_CAP_S", 0),
    # AC-S1.24's seven.
    ("U1", "INTERRUPT_MAX_LATENCY_S", 21),
    ("U2", "ACP_CANCEL_SETTLE_S", 60),
    ("U3", "CANCEL_HOLD_MARGIN_S", 40),
    ("U4", "INTERRUPT_MIN_GAP_S", 29),
    ("U5", "INTERRUPT_BUDGET_WINDOW_S", 149),
    ("U6", "ACP_WRITE_SETTLE_S", 10),
    ("U7", "ACP_KILL_GRACE_S", 110),
]


@pytest.mark.parametrize(("label", "name", "bad"), _ORDERING_MUTANTS)
def test_each_interrupt_ordering_has_a_mutant_that_reddens_it(
    monkeypatch: pytest.MonkeyPatch, label: str, name: str, bad: int
) -> None:
    """One mutant per ordering, each naming the ordering it breaks.

    ``I4-credit`` carries two rows because the equality is violable from either
    side, and the derived form r4 rejected was violable from neither.
    """
    monkeypatch.setattr(timing, name, bad)
    with pytest.raises(ValueError, match=label):
        timing.check_delivery_orderings()


def test_i4_credit_equality_is_not_a_tautology() -> None:
    """The r4 blocker, as a test: the clause must be over LITERALS.

    A derived cap — ``BUSY_CREDIT_CAP_S = IDLE_STALL_AGE_S -
    DELIVERY_MAX_LIFETIME_S - BUSY_CREDIT_MARGIN_S`` — would make the equality
    hold for every value of every term, so this asserts the three terms are
    genuinely independent module-level literals rather than one expression.
    """
    source = Path(timing.__file__).read_text()
    tree = ast.parse(source)
    literals = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        if isinstance(node.value, ast.Constant)
    }
    for name in ("BUSY_CREDIT_CAP_S", "BUSY_CREDIT_MARGIN_S", "DELIVERY_MAX_LIFETIME_S",
                 "IDLE_STALL_AGE_S"):
        assert name in literals, f"{name} must be a plain literal, not a derived expression"
    assert (
        literals["DELIVERY_MAX_LIFETIME_S"]
        + literals["BUSY_CREDIT_CAP_S"]
        + literals["BUSY_CREDIT_MARGIN_S"]
        == literals["IDLE_STALL_AGE_S"]
    )
