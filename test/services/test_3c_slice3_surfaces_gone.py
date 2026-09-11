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


def test_the_seat_returns_before_the_paste_seam(monkeypatch) -> None:
    """The behavioural half of K8, and the one that would actually break.

    ``deliver_pending`` must return for a supervisor-role receiver BEFORE any
    paste call is reachable. Asserted by making the role probe answer True and
    counting calls at the seam — a refactor that moved the gate below the paste
    would pass every grep in this file and fail here.
    """
    from cli_agent_orchestrator.services import inbox_service, terminal_service

    pastes: list[str] = []
    monkeypatch.setattr(
        terminal_service, "send_prepared_input", lambda *a, **k: pastes.append("paste")
    )
    monkeypatch.setattr(terminal_service, "prepare_input", lambda *a, **k: pastes.append("prepare"))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.mailbox_service.probe_supervisor_role",
        lambda tid: True,
    )
    monkeypatch.setattr(inbox_service, "get_pending_messages", lambda *a, **k: [object()])

    try:
        inbox_service.InboxService().deliver_pending("sup-k8")
    except Exception:
        # The gate is what is under test, not the rest of the method's wiring.
        # Any exception AFTER the gate would still have had to pass the seam.
        pass

    assert pastes == [], "a supervisor receiver reached the paste seam"


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
