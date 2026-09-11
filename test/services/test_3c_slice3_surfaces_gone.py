"""AC-3c as a test: the deleted surfaces stay deleted (WP-ARCH 3c slice 3).

The plan states its acceptance for this slice as a set of greps. A grep an
operator runs once is a fact about a moment; the same grep as a test is a fact
about every commit after it, which is the difference between "we deleted it" and
"it is gone". So the acceptance greps live here.

**Why static assertions earn their place in a suite that prefers behaviour.**
Everything below is a DELETION, and a deletion has no behaviour to observe — the
thing that would misbehave is precisely what is absent. The behavioural half is
covered elsewhere (``test/app/delivery/`` for the carrier that replaced these,
``test_adoption.py`` for the rows they used to serve). What these arms catch is
the re-introduction: a future edit that brings back a second carrier, a second
escalation authority, or a flag that can switch the seat's only channel off.

**Every deletion arm is paired with a negative control.** An arm that only
asserts absence passes just as well when the thing it was watching was renamed,
or when the whole module tree moved, or when the grep target was typo'd. So each
group also names something that MUST still be present, and the two wake keys the
3c plan explicitly requires keep resolving are the controls for the flag group.
"""

from __future__ import annotations

from pathlib import Path
from test.services.test_p3b_seat_carrier_positions import (  # noqa: F401
    SEAT_TERMINAL,
    _callback,
    _seat,
    seat_db,
)
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator"


def _live_sources() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def _code_of(path: Path) -> str:
    """The file's CODE, with comments and docstrings removed.

    Via the tokenizer rather than a line filter, because a docstring's BODY lines
    do not start with a quote — a naive filter keeps them and every arm below
    then fails on its own explanatory prose.

    The distinction is the whole point: 3c leaves comments naming the deleted
    surfaces deliberately, so a reader who greps for the old name finds the
    paragraph saying where it went. An arm that counted those would force the
    deletion to be undocumented, which is the opposite of what this repo does.
    """
    import io
    import tokenize

    kept: list[str] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline)
        prev_type = tokenize.INDENT
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and prev_type in (
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.NEWLINE,
                tokenize.NL,
            ):
                continue  # a bare string statement: a docstring
            if tok.type not in (tokenize.NL, tokenize.NEWLINE):
                prev_type = tok.type
            kept.append(tok.string)
    except (tokenize.TokenError, SyntaxError, UnicodeDecodeError):  # pragma: no cover
        return path.read_text(encoding="utf-8")
    return " ".join(kept)


def _hits(needle: str) -> list[str]:
    """Files whose CODE names ``needle``."""
    return [str(path.relative_to(SRC)) for path in _live_sources() if needle in _code_of(path)]


# -- K2 / K3a / K7: the modules ---------------------------------------------


@pytest.mark.parametrize(
    "module",
    ["teammate_push_service", "doorbell_service", "nudge_discipline"],
)
def test_the_deleted_service_modules_are_absent_from_disk(module: str) -> None:
    assert not (SRC / "services" / f"{module}.py").exists()


@pytest.mark.parametrize(
    "module",
    ["teammate_push_service", "doorbell_service", "nudge_discipline"],
)
def test_nothing_imports_a_deleted_service_module(module: str) -> None:
    assert _hits(f"services.{module}") == []
    assert _hits(f"services import {module}") == []


def test_the_modules_that_replaced_them_are_present() -> None:
    """The negative control for the three arms above.

    Without it, deleting ``services/`` entirely would pass every deletion arm in
    this file.
    """
    assert (SRC / "services" / "native_delivery_health.py").exists()
    assert (SRC / "app" / "delivery" / "tick.py").exists()
    assert (SRC / "services" / "queue_carrier.py").exists()


# -- K7: the ladder ----------------------------------------------------------


@pytest.mark.parametrize(
    "symbol",
    [
        "attempt_rung1",
        "attempt_rung2",
        "convergence_tick",
        "DOORBELL_NUDGE_TEXT",
        "ring_supervisor_doorbell",
        "attempt_teammate_push",
        "write_supervisor_callback_notification",
        "is_supervisor_mailbox_pull_terminal",
        "reconcile_pull_mode_notifications",
    ],
)
def test_no_live_code_names_a_deleted_symbol(symbol: str) -> None:
    assert _hits(symbol) == []


def test_delivery_service_is_reduced_to_one_predicate() -> None:
    """K7's shape, and the arm that makes re-growth announce itself."""
    from cli_agent_orchestrator.services import delivery_service

    assert delivery_service.__all__ == ["is_target_confirmed_dead"]


def test_the_surviving_predicate_still_has_its_caller() -> None:
    """The negative control for the reduction: it was kept FOR someone."""
    reconcile = (SRC / "services" / "conversation_reconcile.py").read_text(encoding="utf-8")
    assert "is_target_confirmed_dead" in reconcile


# -- K8: the seat is not reachable from the paste seam -----------------------


def test_the_paste_seam_is_named_by_a_closed_set_of_modules() -> None:
    """K8 kills the seat's REACHABILITY of the paste seam, not the seam.

    **The plan's grep for this AC is too strong as written.** It asks that
    ``send_prepared_input``/``prepare_input`` hit only ``terminal_service`` and
    ``queue_carrier.inject_worker``. But ``deliver_pending``'s WORKER branch
    legitimately pastes — pasting into a worker's composer is what D7 KEEPS — and
    ``providers/codex.py`` reaches the same seam for a human draft. Asserting the
    plan's literal set would forbid the surviving worker carrier, so this arm
    pins a closed set that names those callers explicitly instead. A module
    joining the set is the event worth catching; the seat's own exclusion is
    asserted behaviourally by ``test_the_seat_returns_before_the_paste_seam``.
    """
    callers = set(_hits("send_prepared_input")) | set(_hits("prepare_input"))
    assert callers == {
        "services/terminal_service.py",  # where the seam is defined
        "services/queue_carrier.py",  # the queue's worker injector
        "services/inbox_service.py",  # deliver_pending's WORKER branch
    }, sorted(callers)


def test_the_seat_returns_before_the_paste_seam(seat_db) -> None:
    """The behavioural half of K8, against a REAL seat.

    ``deliver_pending`` must return for a supervisor-role receiver BEFORE any
    paste call is reachable. A refactor that moved the gate below
    ``prepare_input`` would pass every grep in this file and fail here.

    **This arm was vacuous in r1 and the rewrite is the point.** It called
    ``deliver_pending("sup-k8")`` against a database with no such terminal, so
    the method returned at its ``no_terminal_metadata`` guard — dozens of lines
    ABOVE the role gate. ``pastes == []`` held because nothing ran at all, and
    the arm would have stayed green with the gate deleted outright. Stubbing the
    metadata is not enough either: the pending-row sentinel has to survive a real
    grouping pass, so the row must be real too.

    So it now runs on the ``seat_db`` fixture, which seeds the terminal, the
    mailbox and its incarnation, with one genuine PENDING row. And it asserts
    BOTH halves — that the probe was REACHED and that the seam was not — because
    "no paste happened" is exactly the claim an early return also satisfies.
    """
    from unittest.mock import patch

    from cli_agent_orchestrator.services import inbox_service as inbox_mod
    from cli_agent_orchestrator.services import mailbox_service as mailbox_mod

    with seat_db.begin() as db:
        _seat(db)
        _callback(db)

    probed: list[str] = []
    pastes: list[str] = []

    def _probe(terminal_id):
        probed.append(terminal_id)
        return True

    with (
        patch.object(mailbox_mod, "probe_supervisor_role", _probe),
        patch(
            "cli_agent_orchestrator.services.terminal_service.prepare_input",
            side_effect=lambda *a, **k: pastes.append("prepare"),
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.send_prepared_input",
            side_effect=lambda *a, **k: pastes.append("send"),
        ),
        patch.object(inbox_mod, "status_monitor", MagicMock()),
        patch.object(inbox_mod, "provider_manager", MagicMock()),
        patch.object(
            inbox_mod,
            "get_terminal_metadata",
            return_value={
                "tmux_session": "cao-p3b",
                "tmux_window": SEAT_TERMINAL,
                "lifecycle_generation": 1,
                "recovery_state": None,
                "metadata": {},
            },
        ),
    ):
        inbox_mod.InboxService().deliver_pending(SEAT_TERMINAL)

    assert probed == [SEAT_TERMINAL], (
        "the role gate was never reached: deliver_pending returned above it, so "
        "an empty paste list proves nothing about the ban"
    )
    assert pastes == [], "a supervisor receiver reached the paste seam"


def test_the_role_gate_precedes_every_paste_in_deliver_pending() -> None:
    """The ORDER of the gate and the seam, read off the parsed method.

    **Why this is structural and says so.** The arm above proves the gate is
    REACHED on a real seat with a real pending row — which r1's version did not,
    and which is the half that was vacuous. It cannot prove the second half,
    because ``deliver_pending`` does not reach ``prepare_input`` in that harness
    for EITHER role: something downstream of the gate (the attempt open and the
    status gate) stops it first. Measured, not assumed — the worker contrast was
    tried and pastes nothing either, so an arm asserting "no paste" there would
    be asserting the harness's limit, not the ban.

    So the ordering is taken from the AST instead, and that is a real assertion
    rather than a consolation: K8's ban IS an ordering claim — the gate returns
    BEFORE the seam is reachable — and moving the gate below ``prepare_input`` is
    exactly the refactor this has to catch. It changes the parsed order and this
    reddens. Verified against that mutant.
    """
    import ast
    import inspect
    import textwrap

    from cli_agent_orchestrator.services.inbox_service import InboxService

    tree = ast.parse(textwrap.dedent(inspect.getsource(InboxService.deliver_pending)))

    gate_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "probe_supervisor_role"
    ]
    seam_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("prepare_input", "send_prepared_input")
    ]

    assert gate_lines, "deliver_pending no longer consults the role gate at all"
    assert seam_lines, (
        "deliver_pending reaches no paste seam, so this arm is watching nothing — "
        "if the worker path moved, re-point it rather than deleting it"
    )

    # The gate must RETURN for a supervisor receiver — a gate that falls through
    # is the first mutant this pairs with.
    guarded_returns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Call)
        and isinstance(node.test.func, ast.Name)
        and node.test.func.id == "probe_supervisor_role"
        and any(isinstance(stmt, ast.Return) for stmt in node.body)
    ]
    assert guarded_returns, "the role gate no longer RETURNS for a supervisor receiver"

    # And it must sit OUTSIDE every loop that can paste. Textual order is not the
    # property and asserting it was the second mutant's escape: a gate moved to
    # the line directly above ``prepare_input`` is still textually first, and
    # still lets a supervisor row reach the seam on the iteration that gets
    # there. What K8 requires is that the seat leaves the method BEFORE the
    # delivery loop is entered at all.
    def _contains_paste(node: ast.AST) -> bool:
        return any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr in ("prepare_input", "send_prepared_input")
            for inner in ast.walk(node)
        )

    pasting_loops = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.For, ast.While, ast.AsyncFor)) and _contains_paste(node)
    ]
    assert pasting_loops, "no loop in deliver_pending pastes — re-point this arm"

    for loop in pasting_loops:
        nested_gates = [
            node
            for node in ast.walk(loop)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "probe_supervisor_role"
        ]
        assert not nested_gates, (
            "the role gate moved INSIDE a loop that pastes: a supervisor receiver "
            "now reaches the seam on the iteration the gate happens to run in, "
            "which is the ban only by accident"
        )


def test_the_worker_injector_still_exists() -> None:
    """The negative control: K8 removed a path, not the pane carrier."""
    from cli_agent_orchestrator.services.queue_carrier import PaneWorkerInjector

    assert hasattr(PaneWorkerInjector, "inject")


# -- the four flags, and the two that must survive ---------------------------


@pytest.mark.parametrize(
    "env_name,path",
    [
        ("CAO_SUPERVISOR_MAILBOX_PULL", "supervisor.mailbox_pull"),
        ("CAO_W2M_TEAMMATE_PUSH", "supervisor.teammate_push"),
        ("CAO_SUPERVISOR_DOORBELL", "supervisor.doorbell"),
        ("CAO_SUPERVISOR_WAKE_NATIVE", "supervisor.wake.native"),
    ],
)
def test_a_deleted_flag_is_gone_from_the_registry(env_name: str, path: str) -> None:
    """Asserted against ``ENV_REGISTRY``, never through ``ConfigService.get()``.

    ``get()`` answers ``None`` for any unknown dotted path, so it cannot tell a
    deleted key from a typo and would pass for either. The registry is what makes
    a key settable, so the registry is what has to stop naming it.
    """
    from cli_agent_orchestrator.services import config_service as cs

    assert env_name not in cs.ENV_REGISTRY
    assert not [k for k, v in cs.ENV_REGISTRY.items() if v[0] == path]
    assert path not in cs._OWNED_DEFAULTS


@pytest.mark.parametrize(
    "env_name,path",
    [
        ("CAO_SUPERVISOR_WAKE_MAX_RECORD_AGE_S", "supervisor.wake.max_record_age_s"),
        ("CAO_SUPERVISOR_WAKE_DEDUPE_WINDOW", "supervisor.wake.dedupe_window"),
    ],
)
def test_the_two_wake_keys_the_plan_protects_still_resolve(env_name: str, path: str) -> None:
    """The negative control the 3c plan names explicitly.

    Both sit in the same ``supervisor.wake.*`` block as the deleted
    ``supervisor.wake.native``, so a sweep by prefix rather than by key takes
    them too — and the plan says §7c FAILS if they stop resolving.
    """
    from cli_agent_orchestrator.services import config_service as cs

    assert env_name in cs.ENV_REGISTRY
    assert cs.ENV_REGISTRY[env_name][0] == path
    assert cs.ENV_REGISTRY[env_name][2] is not None
